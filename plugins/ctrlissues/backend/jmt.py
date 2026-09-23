"""Source-derived semantic evaluator; native lifecycle and hook code omitted."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import hashlib
import json
import math
import os
import platform
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

def _load_sibling(name):
    spec = importlib.util.spec_from_file_location('jmt_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
decisions = _load_sibling('decisions')
API_URL = 'https://api.typesafe.ai/v1/systemone'
POLICY_PATH = Path(__file__).with_name('policy.json')
POLICY = json.loads(POLICY_PATH.read_text(encoding='utf-8'))
MODEL = POLICY['model']
ENGINE_SHA256 = hashlib.sha256(b''.join((Path(__file__).with_name(n).read_bytes() for n in ('jmt.py', 'policy.json', 'decisions.py', 'selection.py', 'bindings.py')))).hexdigest()
bindings = _load_sibling('bindings')
MAX_STATE = 32768
MAX_RESPONSE = 32768
DEFAULTS = {'cloud_enabled': False, 'mode': 'observe', 'credential_runner': [], 'constraints': [], 'max_calls_per_turn': 32}
SECRET_FIELD = re.compile('(?i)^(?:.*[_-])?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization|cookie|private[_-]?key|credentials?)$')
ASSIGNMENT = re.compile('(?ix)(\\b[a-z0-9_-]*?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization|cookie)\\b["\']?\\s*[:=]\\s*)(?:"[^"\\n]*"|\'[^\'\\n]*\'|[^\\s,;}]+)')
TOKEN = re.compile('\\b(?:sk[-_][A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|AKIA[A-Z0-9]{16})\\b')
PRIVATE_KEY = re.compile('-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)

def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')

def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()

def redact(value: Any) -> Any:
    """Best-effort evidence redaction, not a general secret detector."""
    if isinstance(value, dict):
        return {k: '[REDACTED]' if SECRET_FIELD.fullmatch(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    value = PRIVATE_KEY.sub('[REDACTED PRIVATE KEY]', value)
    value = TOKEN.sub('[REDACTED]', value)
    value = re.sub('(?i)\\bBearer\\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', value)
    value = ASSIGNMENT.sub(lambda m: m.group(1) + '[REDACTED]', value)
    for name, secret in os.environ.items():
        if SECRET_FIELD.fullmatch(name) and len(secret) >= 8:
            value = value.replace(secret, '[REDACTED]')
    return value

def verdict(decision: str, reason: str | list[str], state: Any, *, probabilities: dict | None=None, usage: dict | None=None, latency: float=0, basis: str='local', model: str | None=None) -> dict:
    return {'decision': decision, 'basis': basis, 'reason_codes': [reason] if isinstance(reason, str) else reason, 'probabilities': probabilities or {}, 'model': model, 'latency_ms': round(latency, 2), 'usage': usage or {}, 'policy_sha256': digest(POLICY), 'state_sha256': digest(redact(state)), 'engine_sha256': ENGINE_SHA256}

def strings(value: Any, limit: int=32) -> bool:
    return isinstance(value, list) and len(value) <= limit and all((isinstance(v, str) for v in value))

def valid_state(state: Any, mode: str) -> bool:
    if not isinstance(state, dict) or not strings(state.get('evidence', [])):
        return False
    if mode == 'verify':
        return isinstance(state.get('claim'), str)
    action = state.get('action')
    return isinstance(state.get('goal'), str) and bool(state['goal'].strip()) and strings(state.get('constraints', [])) and isinstance(action, dict) and isinstance(action.get('tool'), str) and bool(action['tool']) and isinstance(action.get('arguments'), dict)

def readonly(action: dict) -> bool:
    """Only literal pwd/git-status forms bypass the remote judgment."""
    if action.get('tool') not in ('Bash', 'exec_command', 'shell_command', 'shell', 'exec_argv'):
        return False
    args = action.get('arguments', {})
    if action['tool'] == 'exec_argv':
        tokens = args.get('argv')
        if not strings(tokens):
            return False
    else:
        command = args.get('command', args.get('cmd'))
        if not isinstance(command, str) or re.search('[;$`|&<>\\n\\r(){}]', command):
            return False
        try:
            tokens = shlex.split(command)
        except ValueError:
            return False
    return tokens in (['pwd'], ['git', 'status'], ['git', 'status', '--short'], ['git', 'status', '--porcelain'])

def literal_authorized(state: dict, cfg: dict) -> bool:
    """Only an exact positive goal/action pair can bypass semantic judgment."""
    if cfg.get('constraints') or state.get('constraints'):
        return False
    action = state.get('action', {})
    if not readonly(action):
        return False
    args = action['arguments']
    command = ' '.join(args['argv']) if action['tool'] == 'exec_argv' else args.get('command', args.get('cmd', '')).strip()
    goal = state.get('goal', '').strip().lower().rstrip('.')
    pairs = {('print the current directory', 'pwd'), ('show the current directory', 'pwd'), ('show git status', 'git status'), ('show git status --short', 'git status --short'), ('show git status --porcelain', 'git status --porcelain')}
    return (goal, command) in pairs

def eligibility_reason(state: Any, mode: str, cfg: dict) -> str | None:
    if mode not in ('check', 'verify') or not valid_state(state, mode):
        return 'malformed_state'
    if mode == 'check':
        tool, args = (state['action']['tool'], state['action']['arguments'])
        if tool in ('Bash', 'exec_command', 'shell_command', 'shell', 'apply_patch'):
            if not isinstance(args.get('command', args.get('cmd')), str):
                return 'malformed_tool_arguments'
        elif tool == 'exec_argv':
            if not strings(args.get('argv')) or not args['argv']:
                return 'malformed_tool_arguments'
        elif tool in ('write_stdin', 'functions.write_stdin'):
            return 'uncovered_continuation'
        elif tool in ('collaborationspawn_agent', 'collaborationfollowup_task', 'collaborationsend_message', 'collaboration.spawn_agent', 'collaboration.followup_task', 'collaboration.send_message'):
            if not isinstance(args, dict):
                return 'malformed_tool_arguments'
            if tool.endswith('spawn_agent') and (not isinstance(args.get('task_name'), str)):
                return 'malformed_tool_arguments'
            if tool.endswith('send_message') and (not isinstance(args.get('message'), str)):
                return 'malformed_tool_arguments'
        elif tool in ('list_agents', 'wait_agent', 'view_image', 'collaboration.list_agents', 'collaboration.wait_agent', 'collaboration_list_agents', 'collaboration_wait_agent'):
            pass
        elif not tool.startswith('mcp__'):
            return 'unsupported_tool'
        clean = redact(state)
        merged = dict(clean)
        merged['constraints'] = clean.get('constraints', []) + redact(cfg.get('constraints', []))
        if len(canonical(merged)) > MAX_STATE:
            return 'state_bound'
    elif len(canonical(redact(state))) > MAX_STATE:
        return 'state_bound'
    return None

class NoRedirect(HTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirect_refused')

def probability(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and (0 <= value <= 1)

def validate_response(result: Any, questions: dict) -> dict:
    if not isinstance(result, dict) or result.get('model') != MODEL:
        raise ValueError('model_mismatch')
    answers = result.get('answers')
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError('answer_keys')
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get('type') != question['type']:
            raise ValueError('answer_type')
        if question['type'] == 'noul':
            if not probability(answer.get('noul')):
                raise ValueError('invalid_probability')
        else:
            if question['type'] == 'score':
                decisions._answer(question, answer)
                continue
            probs = answer.get('probabilities')
            if not isinstance(probs, dict) or set(probs) != set(question['criteria']) or (not all((probability(v) for v in probs.values()))) or (abs(sum(probs.values()) - 1) > 0.02) or (answer.get('choice') not in probs) or (not probability(answer.get('confidence'))):
                raise ValueError('invalid_choice')
            if probs[answer['choice']] + 0.001 < max(probs.values()):
                raise ValueError('choice_not_max')
    usage = result.get('usage')
    if not isinstance(usage, dict) or not all((type(usage.get(k)) is int and usage[k] >= 0 for k in ('input_tokens', 'output_tokens'))):
        raise ValueError('invalid_usage')
    return result

def request_api(state: dict, questions: dict, transport: Callable | None=None, deadline: float | None=None) -> dict:
    payload = {'state': state, 'model': MODEL, 'questions': questions}
    if transport is not None:
        return validate_response(transport(payload), questions)
    key = os.environ.get('TYPESAFE_API_KEY')
    if not key:
        raise RuntimeError('missing_api_key')
    req = Request(API_URL, data=canonical(payload), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}, method='POST')
    opener = build_opener(NoRedirect())
    deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + 6)
    for attempt in range(2):
        if time.monotonic() >= deadline:
            raise TimeoutError('assessment_deadline')
        try:
            with opener.open(req, timeout=max(0.1, min(4, deadline - time.monotonic()))) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ValueError('response_bound')
                return validate_response(json.loads(raw), questions)
        except HTTPError as error:
            if error.code in (429, 529) and attempt == 0 and (deadline - time.monotonic() > 0.8):
                error.close()
                time.sleep(0.3)
                continue
            code = error.code
            error.close()
            raise RuntimeError('provider_http_' + str(code)) from None
    raise RuntimeError('provider_unavailable')

def evaluate(state: Any, config: dict | None=None, transport: Callable | None=None, mode: str='check') -> dict:
    cfg = dict(DEFAULTS) | (config or {})
    started = time.monotonic()
    try:
        reason = eligibility_reason(state, mode, cfg)
        if reason:
            return verdict('unassessed', reason, {})
        clean = redact(state)
        if mode == 'check':
            clean['constraints'] = clean.get('constraints', []) + redact(cfg['constraints'])
        if len(canonical(clean)) > MAX_STATE:
            return verdict('unassessed', 'state_bound', {})
        if mode == 'check' and literal_authorized(clean, cfg):
            return verdict('pass', 'literal_read', clean)
        if not cfg['cloud_enabled'] and transport is None:
            return verdict('unassessed', 'cloud_disabled', clean)
        deadline = time.monotonic() + 6
        responses, errors = ([], [])
        request_count = 0

        def query(job):
            try:
                return request_api(job[1], job[2], transport, deadline=deadline)
            except (RuntimeError, ValueError, TypeError, KeyError, OSError, URLError, TimeoutError):
                return None

        def collect(jobs):
            nonlocal request_count
            request_count += len(jobs)
            if len(jobs) <= 1:
                replies = [query(job) for job in jobs]
            else:
                with ThreadPoolExecutor(max_workers=POLICY['bindings']['parallel_requests']) as pool:
                    replies = list(pool.map(query, jobs))
            errors.extend((job[0] for job, result in zip(jobs, replies) if result is None))
            responses.extend((result for result in replies if result is not None))
            return replies
        rule_results, instruction_checks = ([], [])
        probs, reasons, decision = ({}, [], 'pass')
        if mode == 'verify':
            reply = collect([('verification', clean, POLICY['verification'])])[0]
            if reply is not None:
                answer = reply['answers']['support']
                probs = answer['probabilities']
                choice = answer['choice']
                decision = 'block' if choice == 'contradicted' and probs[choice] >= POLICY['thresholds']['block'] else 'pass' if choice in ('supported', 'no_claim') and probs[choice] >= 0.7 else 'review'
                reasons = [choice]
        else:
            blocks = bindings.rule_blocks(clean)
            if len(blocks) > POLICY['rules']['max_rules']:
                return verdict('unassessed', 'instruction_count_bound', clean)
            gates = bindings.prepare(clean, POLICY['bindings'])
            jobs = [(f'numeric_rule_{i}', bindings.request_state(gate, clean), bindings.question_set(gate, POLICY['bindings'])) for i, gate in enumerate(gates)]
            if gates:
                authority_state, authority_questions = bindings.authority_request(gates, clean, POLICY['bindings'])
                jobs.append(('rule_authority', authority_state, authority_questions))
            replies = collect(jobs)
            authority = replies[-1] if gates else None
            cleared = set()
            for i, (gate, reply) in enumerate(zip(gates, replies)):
                if reply is None:
                    continue
                record = bindings.compose([gate], reply['answers'], POLICY['bindings'])[0]
                active = authority['answers'][f'a{i}'] if authority else None
                record['signals']['authority'] = active
                record['enforced'] = bool(record['enforced'] and active and (active['choice'] == 'active') and (active['probabilities']['active'] >= POLICY['bindings']['authority_min']))
                resolved = bindings.resolved_without_violation(record, POLICY['bindings'], POLICY['rules'])
                record['resolved_without_violation'] = resolved
                if resolved:
                    cleared.add((gate['source'], gate['rule']))
                rule_results.append(record)
            if any((record['enforced'] for record in rule_results)):
                decision, reasons = ('block', ['numeric_rule_violation'])
            else:
                remaining = [block for block in blocks if (block['source'], block['text']) not in cleared]
                atomic_state, questions = bindings.atomic_request(remaining, clean, POLICY['rules'])
                questions.update({name: question for name, question in POLICY['questions'].items() if name != 'scope_conflict'})
                reply = collect([('remaining_instructions', atomic_state, questions)])[0]
                if reply is not None:
                    for name in ('irreversible_without_basis', 'failed_candidate'):
                        value = reply['answers'][name]['noul']
                        probs[name] = value
                        if value >= POLICY['thresholds']['block']:
                            decision = 'block'
                            reasons.append(name)
                        elif value >= POLICY['thresholds']['review']:
                            if decision != 'block':
                                decision = 'review'
                            reasons.append(name)
                    for i, block in enumerate(remaining):
                        answer = reply['answers'][f'r{i}']
                        violation = answer['probabilities']['violated']
                        unknown = answer['probabilities']['unknown']
                        disposition = 'block' if violation >= POLICY['rules']['block_min'] else 'review' if max(violation, unknown) >= POLICY['rules']['review_min'] else 'pass'
                        instruction_checks.append({**block, 'answer': answer, 'disposition': disposition})
                        if disposition == 'block':
                            decision = 'block'
                            reasons.append('instruction_conflict')
                        elif disposition == 'review':
                            if decision != 'block':
                                decision = 'review'
                            reasons.append('instruction_uncertain')
        if errors and decision != 'block':
            decision, reasons = ('unassessed', ['partial_assessment_unavailable'])
        usage = {k: sum((r['usage'][k] for r in responses)) for k in ('input_tokens', 'output_tokens')}
        output = verdict(decision, list(dict.fromkeys(reasons)) or ['no_conflict_detected'], clean, probabilities=probs, usage=usage, latency=(time.monotonic() - started) * 1000, basis='typesafe' if responses else 'local', model=MODEL if responses else None)
        output.update(rule_checks=rule_results, instruction_checks=instruction_checks, api_requests=request_count, assessment_complete=not errors, unavailable_checks=errors, short_circuit=bool(mode == 'check' and any((r['enforced'] for r in rule_results))))
        return output
    except (RuntimeError, ValueError, TypeError, KeyError, OSError, URLError, TimeoutError):
        return verdict('unassessed', 'provider_or_input_unavailable', {}, latency=(time.monotonic() - started) * 1000)
