use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashSet},
    fs,
    io::{self, Read},
    path::Path,
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};
const SCHEMA: u32 = 1;
const MAX: usize = 4096;
#[derive(Clone, Serialize, Deserialize)]
struct Blueprint {
    objective: String,
    components: Vec<String>,
    layers: Vec<String>,
    next: String,
    #[serde(default)]
    dependencies: BTreeMap<String, Vec<String>>,
}
#[derive(Clone, Serialize, Deserialize)]
struct Cursor {
    layer: Option<String>,
    incomplete_components: Vec<String>,
    next: String,
}
#[derive(Clone, Serialize, Deserialize)]
struct Issue {
    component: String,
    reason: String,
    resolved: bool,
}
#[derive(Clone, Serialize, Deserialize)]
struct Receipt {
    kind: String,
    label: String,
    argv_sha256: String,
    generation: u64,
    status: String,
    exit_code: Option<i32>,
    #[serde(default)]
    component: Option<String>,
    #[serde(default)]
    component_epoch: u64,
    #[serde(default)]
    cwd: Option<String>,
}
#[derive(Clone, Serialize, Deserialize)]
struct Session {
    schema: u32,
    session_id: String,
    phase: String,
    generation: u64,
    blueprint: Option<Blueprint>,
    cursor: Cursor,
    marks: BTreeMap<String, BTreeMap<String, String>>,
    issues: Vec<Issue>,
    receipts: Vec<Receipt>,
    proof_generation: Option<u64>,
    #[serde(default)]
    revision: u64,
    #[serde(default)]
    input_epochs: BTreeMap<String, u64>,
}
#[derive(Deserialize)]
struct Revise {
    expected_revision: u64,
    reason: String,
    blueprint: Blueprint,
    #[serde(default)]
    invalidate: BTreeMap<String, String>,
}
pub struct Store {
    c: Connection,
    id: String,
    data: std::path::PathBuf,
    expected_revision: Option<u64>,
}
fn resolve_alias(c: &Connection, id: &str) -> Result<String, Box<dyn std::error::Error>> {
    let mut current = id.to_owned();
    let mut seen = HashSet::new();
    loop {
        if !seen.insert(current.clone()) {
            return Err("session alias cycle".into());
        }
        let next: Option<String> = c
            .query_row(
                "SELECT canonical FROM session_aliases WHERE alias=?",
                [&current],
                |r| r.get(0),
            )
            .optional()?;
        match next {
            Some(x) => current = x,
            None => break,
        }
    }
    Ok(current)
}
impl Store {
    pub fn open(d: &Path, id: &str) -> Result<Self, Box<dyn std::error::Error>> {
        if id.trim().is_empty() || id.len() > 256 {
            return Err("valid session_id required".into());
        }
        crate::private_fs::create_private_dir_all(d)?;
        let p = d.join("state.sqlite");
        let c = Connection::open(&p)?;
        perm(&p, 0o600)?;
        c.busy_timeout(std::time::Duration::from_secs(5))?;
        c.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS graphfather_schema(version INTEGER NOT NULL); INSERT INTO graphfather_schema(version) SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM graphfather_schema); CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, document TEXT NOT NULL); CREATE TABLE IF NOT EXISTS session_aliases(alias TEXT PRIMARY KEY, canonical TEXT NOT NULL); CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, at INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, generation INTEGER NOT NULL, cwd TEXT NOT NULL, tool_name TEXT NOT NULL, input_sha256 TEXT NOT NULL, outcome_sha256 TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('success','failed')), at INTEGER NOT NULL); CREATE INDEX IF NOT EXISTS observations_lookup ON observations(session_id,generation,cwd,tool_name,input_sha256,id);")?;
        let v: i64 = c.query_row("SELECT version FROM graphfather_schema", [], |r| r.get(0))?;
        if v != SCHEMA as i64 {
            return Err("unsupported state schema".into());
        }
        let canonical = resolve_alias(&c, id)?;
        Ok(Self {
            c,
            id: canonical,
            data: fs::canonicalize(d)?,
            expected_revision: None,
        })
    }
    pub fn canonical_id(&self) -> &str {
        &self.id
    }
    pub fn register_handoff(&mut self, origin: &str) -> Result<(), Box<dyn std::error::Error>> {
        if origin.trim().is_empty() || origin == self.id || origin.len() > 256 {
            return Ok(());
        }
        let tx = self
            .c
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let canonical = resolve_alias(&tx, origin)?;
        let raw: Option<String> = tx
            .query_row(
                "SELECT document FROM sessions WHERE id=?",
                [&canonical],
                |r| r.get(0),
            )
            .optional()?;
        let Some(raw) = raw else {
            tx.commit()?;
            return Ok(());
        };
        let source: Session = serde_json::from_str(&raw)?;
        if source.blueprint.is_none() {
            tx.commit()?;
            return Ok(());
        }
        if canonical == self.id {
            tx.commit()?;
            return Ok(());
        }
        let destination: Option<String> = tx
            .query_row(
                "SELECT document FROM sessions WHERE id=?",
                [&self.id],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(raw) = destination {
            let existing: Session = serde_json::from_str(&raw)?;
            if existing.blueprint.is_some() {
                return Err("handoff destination already planned".into());
            }
        }
        let existing: Option<String> = tx
            .query_row(
                "SELECT canonical FROM session_aliases WHERE alias=?",
                [&self.id],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(x) = existing {
            if resolve_alias(&tx, &x)? != canonical {
                return Err("session alias conflict".into());
            }
        } else {
            tx.execute(
                "INSERT INTO session_aliases(alias,canonical) VALUES(?,?)",
                params![self.id, canonical],
            )?;
        }
        tx.commit()?;
        self.id = canonical;
        Ok(())
    }
    pub fn set_expected_revision(&mut self, revision: Option<u64>) {
        self.expected_revision = revision;
    }
    fn empty(&self) -> Session {
        Session {
            schema: SCHEMA,
            session_id: self.id.clone(),
            phase: "blueprint".into(),
            generation: 0,
            blueprint: None,
            cursor: Cursor {
                layer: None,
                incomplete_components: vec![],
                next: "plan a blueprint".into(),
            },
            marks: BTreeMap::new(),
            issues: vec![],
            receipts: vec![],
            proof_generation: None,
            revision: 0,
            input_epochs: BTreeMap::new(),
        }
    }
    fn load(&self) -> Result<Session, Box<dyn std::error::Error>> {
        let x: Option<String> = self
            .c
            .query_row(
                "SELECT document FROM sessions WHERE id=?",
                [&self.id],
                |r| r.get(0),
            )
            .optional()?;
        match x {
            None => Ok(self.empty()),
            Some(x) => {
                let s: Session = serde_json::from_str(&x)?;
                if s.schema != SCHEMA || s.session_id != self.id {
                    return Err("invalid session document".into());
                }
                Ok(Session {
                    revision: s.revision,
                    input_epochs: s.input_epochs,
                    ..s
                })
            }
        }
    }
    fn mutate<F>(&mut self, k: &str, data: Value, f: F) -> Result<Value, Box<dyn std::error::Error>>
    where
        F: FnOnce(&mut Session) -> Result<(), Box<dyn std::error::Error>>,
    {
        let empty = self.empty();
        let tx = self
            .c
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let x: Option<String> = tx
            .query_row(
                "SELECT document FROM sessions WHERE id=?",
                [&self.id],
                |r| r.get(0),
            )
            .optional()?;
        let mut s = match x {
            Some(x) => serde_json::from_str(&x)?,
            None => empty,
        };
        if s.schema != SCHEMA || s.session_id != self.id {
            return Err("invalid session document".into());
        };
        if k != "check_finished" {
            if let Some(expected) = self.expected_revision {
                if s.revision != expected {
                    return Err("stale revision".into());
                }
            }
        }
        let before = serde_json::to_string(&s)?;
        f(&mut s)?;
        if serde_json::to_string(&s)? == before {
            let path = publish_tx(&tx, &self.id, &self.data, &s)?;
            tx.commit()?;
            return Ok(with_path(view(&s), path));
        }
        tx.execute("INSERT INTO sessions(id,document)VALUES(?,?) ON CONFLICT(id)DO UPDATE SET document=excluded.document",params![self.id,serde_json::to_string(&s)?])?;
        tx.execute(
            "INSERT INTO events(session_id,at,kind,data)VALUES(?,?,?,?)",
            params![
                self.id,
                now(),
                k,
                serde_json::to_string(&if k == "revise" {
                    json!({"reason":data["reason"],"old":before})
                } else {
                    data
                })?
            ],
        )?;
        let path = publish_tx(&tx, &self.id, &self.data, &s)?;
        tx.commit()?;
        Ok(with_path(view(&s), path))
    }
    pub fn status(&mut self) -> Result<Value, Box<dyn std::error::Error>> {
        let empty = self.empty();
        let tx = self
            .c
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let raw: Option<String> = tx
            .query_row(
                "SELECT document FROM sessions WHERE id=?",
                [&self.id],
                |r| r.get(0),
            )
            .optional()?;
        let s = match raw {
            Some(x) => serde_json::from_str(&x)?,
            None => empty,
        };
        if s.blueprint.is_none() {
            tx.commit()?;
            let mut v = view(&s);
            v["plan_path"] = Value::Null;
            return Ok(v);
        }
        let path = publish_tx(&tx, &self.id, &self.data, &s)?;
        tx.commit()?;
        Ok(with_path(view(&s), path))
    }
    pub fn compact_status(&mut self) -> Result<Value, Box<dyn std::error::Error>> {
        let s = self.status()?;
        let receipts = s["receipts"].as_array().cloned().unwrap_or_default();
        let mut recent: Vec<Value> = receipts.into_iter().rev().take(8).collect();
        recent.reverse();
        recent.extend(self.recent_observations(8)?);
        if recent.len() > 8 {
            recent.drain(0..recent.len() - 8);
        }
        Ok(
            json!({"schema":"ctrlissues.cursor.v1","session_id":s["session_id"],"revision":s["revision"],"generation":s["generation"],"phase":s["phase"],"cursor":s["cursor"],"objective":s.pointer("/blueprint/objective").and_then(Value::as_str).unwrap_or(""),"plan_path":s["plan_path"],"recent_receipts":recent}),
        )
    }
    pub fn completed_observation(
        &self,
        tool_name: &str,
        tool_input: &Value,
        cwd: &str,
    ) -> Result<Option<Value>, Box<dyn std::error::Error>> {
        let state = self.load()?;
        if state.blueprint.is_none() {
            return Ok(None);
        }
        let input_sha256 = observation_input_hash(tool_name, tool_input, cwd)?;
        let row: Option<(i64, String, String)> = self
            .c
            .query_row(
                "SELECT id,status,outcome_sha256 FROM observations WHERE session_id=? AND generation=? AND cwd=? AND tool_name=? AND input_sha256=? ORDER BY id DESC LIMIT 1",
                params![self.id, state.generation, cwd, tool_name, input_sha256],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        Ok(row.map(|(id, status, outcome)| {
            observation_view(
                id,
                &self.id,
                state.generation,
                cwd,
                tool_name,
                &input_sha256,
                &outcome,
                &status,
            )
        }))
    }
    pub fn record_observation(
        &mut self,
        tool_name: &str,
        tool_input: &Value,
        cwd: &str,
        outcome: &Value,
        succeeded: bool,
    ) -> Result<Option<Value>, Box<dyn std::error::Error>> {
        let input_sha256 = observation_input_hash(tool_name, tool_input, cwd)?;
        let outcome_sha256 = hex(&Sha256::digest(serde_json::to_vec(outcome)?));
        let status = if succeeded { "success" } else { "failed" };
        let empty = self.empty();
        let tx = self
            .c
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let raw: Option<String> = tx
            .query_row(
                "SELECT document FROM sessions WHERE id=?",
                [&self.id],
                |r| r.get(0),
            )
            .optional()?;
        let state = match raw {
            Some(raw) => serde_json::from_str::<Session>(&raw)?,
            None => empty,
        };
        if state.schema != SCHEMA || state.session_id != self.id {
            return Err("invalid session document".into());
        }
        if state.blueprint.is_none() {
            tx.commit()?;
            return Ok(None);
        }
        tx.execute(
            "INSERT INTO observations(session_id,generation,cwd,tool_name,input_sha256,outcome_sha256,status,at) VALUES(?,?,?,?,?,?,?,?)",
            params![self.id, state.generation, cwd, tool_name, input_sha256, outcome_sha256, status, now()],
        )?;
        let id = tx.last_insert_rowid();
        let receipt = observation_view(
            id,
            &self.id,
            state.generation,
            cwd,
            tool_name,
            &input_sha256,
            &outcome_sha256,
            status,
        );
        tx.execute(
            "INSERT INTO events(session_id,at,kind,data) VALUES(?,?,?,?)",
            params![
                self.id,
                now(),
                "observation_completed",
                serde_json::to_string(&receipt)?
            ],
        )?;
        tx.commit()?;
        Ok(Some(receipt))
    }
    fn recent_observations(&self, limit: usize) -> Result<Vec<Value>, Box<dyn std::error::Error>> {
        let state = self.load()?;
        if state.blueprint.is_none() || limit == 0 {
            return Ok(vec![]);
        }
        let mut q = self.c.prepare(
            "SELECT id,cwd,tool_name,input_sha256,outcome_sha256,status FROM observations WHERE session_id=? AND generation=? ORDER BY id DESC LIMIT ?",
        )?;
        let rows = q.query_map(params![self.id, state.generation, limit as i64], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
            ))
        })?;
        let mut receipts = Vec::new();
        for row in rows {
            let (id, cwd, tool, input, outcome, status) = row?;
            receipts.push(observation_view(
                id,
                &self.id,
                state.generation,
                &cwd,
                &tool,
                &input,
                &outcome,
                &status,
            ));
        }
        receipts.reverse();
        Ok(receipts)
    }
    pub fn plan(&mut self, file: &str) -> Result<Value, Box<dyn std::error::Error>> {
        let bytes = if file == "-" {
            let mut x = Vec::new();
            io::stdin().take(131073).read_to_end(&mut x)?;
            if x.len() > 131072 {
                return Err("plan input exceeds 128 KiB".into());
            };
            x
        } else {
            fs::read(file)?
        };
        let b: Blueprint = serde_json::from_slice(&bytes)?;
        valid(&b)?;
        validate_dependencies(&b)?;
        self.mutate(
            "plan",
            json!({"objective_length":b.objective.len()}),
            move |s| {
                if s.blueprint.is_some() {
                    return Err("blueprint already exists; use reset".into());
                }
                s.phase = "build".into();
                s.cursor = Cursor {
                    layer: Some(b.layers[0].clone()),
                    incomplete_components: b.components.clone(),
                    next: b.next.clone(),
                };
                s.blueprint = Some(b);
                s.revision = s.revision.saturating_add(1).max(1);
                Ok(())
            },
        )
    }
    pub fn revise(&mut self, file: &str) -> Result<Value, Box<dyn std::error::Error>> {
        let mut bytes = Vec::new();
        if file == "-" {
            io::stdin().take(131073).read_to_end(&mut bytes)?;
        } else {
            bytes = fs::read(file)?;
        }
        if bytes.len() > 131072 {
            return Err("revise input exceeds 128 KiB".into());
        }
        let req: Revise = serde_json::from_slice(&bytes)?;
        valid(&req.blueprint)?;
        validate_dependencies(&req.blueprint)?;
        bound(&req.reason, MAX, "reason")?;
        for (c, l) in &req.invalidate {
            component(&req.blueprint, c)?;
            if !req.blueprint.layers.iter().any(|x| x == l) {
                return Err("invalidate layer unknown".into());
            }
        }
        self.mutate("revise", json!({"reason":req.reason}), move |s| {
            if s.revision != req.expected_revision {
                return Err("stale revision".into());
            }
            let prior = s.blueprint.clone().ok_or("plan a blueprint first")?;
            if prior.objective == req.blueprint.objective
                && prior.components == req.blueprint.components
                && prior.layers == req.blueprint.layers
                && prior.next == req.blueprint.next
                && prior.dependencies == req.blueprint.dependencies
                && req.invalidate.is_empty()
            {
                return Ok(());
            }
            let next_only = prior.objective == req.blueprint.objective
                && prior.components == req.blueprint.components
                && prior.layers == req.blueprint.layers
                && prior.dependencies == req.blueprint.dependencies
                && req.invalidate.is_empty();
            s.revision = s.revision.saturating_add(1);
            s.blueprint = Some(req.blueprint.clone());
            if next_only {
                s.cursor.next = req.blueprint.next.clone();
                return Ok(());
            }
            s.marks
                .retain(|l, _| l == "repair" || req.blueprint.layers.contains(l));
            for m in s.marks.values_mut() {
                m.retain(|c, _| req.blueprint.components.contains(c));
            }
            let mut affected: BTreeMap<String, usize> = BTreeMap::new();
            for (c, l) in &req.invalidate {
                affected.insert(
                    c.clone(),
                    req.blueprint.layers.iter().position(|x| x == l).unwrap(),
                );
            }
            for c in req
                .blueprint
                .components
                .iter()
                .filter(|c| !prior.components.contains(c))
            {
                affected.entry(c.clone()).or_insert(0);
            }
            for c in prior
                .components
                .iter()
                .filter(|c| !req.blueprint.components.contains(c))
            {
                for d in dependent_closure(c, &prior, &req.blueprint) {
                    affected
                        .entry(d)
                        .and_modify(|x| *x = (*x).min(0))
                        .or_insert(0);
                }
            }
            for c in req.blueprint.components.iter() {
                if prior.dependencies.get(c) != req.blueprint.dependencies.get(c) {
                    affected
                        .entry(c.clone())
                        .and_modify(|x| *x = (*x).min(0))
                        .or_insert(0);
                }
            }
            let seeds: Vec<String> = affected.keys().cloned().collect();
            for c in seeds {
                let at = *affected.get(&c).unwrap();
                for d in dependent_closure(&c, &prior, &req.blueprint) {
                    let e = affected.entry(d).or_insert(at);
                    *e = (*e).min(at);
                }
            }
            for (c, at) in affected {
                for l in req.blueprint.layers.iter().skip(at) {
                    if let Some(m) = s.marks.get_mut(l) {
                        m.remove(&c);
                    }
                }
                if let Some(m) = s.marks.get_mut("repair") {
                    m.remove(&c);
                }
                *s.input_epochs.entry(c).or_insert(0) += 1;
            }
            s.issues
                .retain(|i| req.blueprint.components.contains(&i.component));
            s.generation += 1;
            s.proof_generation = None;
            let next = req
                .blueprint
                .layers
                .iter()
                .find(|l| {
                    req.blueprint
                        .components
                        .iter()
                        .any(|c| s.marks.get(*l).and_then(|m| m.get(c)).is_none())
                })
                .cloned();
            if let Some(l) = next {
                s.phase = "build".into();
                s.cursor = Cursor {
                    layer: Some(l.clone()),
                    incomplete_components: req
                        .blueprint
                        .components
                        .iter()
                        .filter(|c| s.marks.get(&l).and_then(|m| m.get(*c)).is_none())
                        .cloned()
                        .collect(),
                    next: req.blueprint.next.clone(),
                };
            } else if s.issues.iter().any(|i| !i.resolved) {
                s.phase = "repair".into();
                s.cursor.layer = None;
                s.cursor.incomplete_components = vec![];
                s.cursor.next = "resolve open repair issues".into();
            } else {
                s.phase = "proof".into();
                s.cursor.layer = None;
                s.cursor.incomplete_components = vec![];
                s.cursor.next = req.blueprint.next.clone();
            }
            Ok(())
        })
    }
    pub fn cursor(&mut self, t: String) -> Result<Value, Box<dyn std::error::Error>> {
        bound(&t, MAX, "cursor")?;
        self.mutate("cursor", json!({"length":t.len()}), move |s| {
            project(s)?;
            s.cursor.next = t;
            Ok(())
        })
    }
    pub fn mark(&mut self, c: &str, e: &str) -> Result<Value, Box<dyn std::error::Error>> {
        bound(e, 8192, "evidence")?;
        let c: String = c.into();
        self.mutate(
            "mark",
            json!({"component":c,"evidence_length":e.len()}),
            move |s| {
                let b = project(s)?.clone();
                component(&b, &c)?;
                if s.phase == "build" {
                    let l = s.cursor.layer.clone().ok_or("current layer missing")?;
                    let m = s.marks.entry(l).or_default();
                    m.insert(c, e.into());
                    s.cursor.incomplete_components = b
                        .components
                        .iter()
                        .filter(|x| !m.contains_key(*x))
                        .cloned()
                        .collect()
                } else if s.phase == "repair" {
                    s.issues
                        .iter_mut()
                        .find(|x| x.component == c && !x.resolved)
                        .ok_or("no open repair issue for component")?
                        .resolved = true;
                    s.marks
                        .entry("repair".into())
                        .or_default()
                        .insert(c, e.into());
                } else {
                    return Err("mark only during build or repair".into());
                }
                Ok(())
            },
        )
    }
    pub fn advance(&mut self) -> Result<Value, Box<dyn std::error::Error>> {
        self.mutate("advance", json!({}), |s| {
            let b = project(s)?.clone();
            if s.phase == "repair" {
                if s.issues.iter().any(|x| !x.resolved) {
                    return Err("open repair issues remain".into());
                }
                s.phase = "proof".into();
                s.cursor.next = "run a whole check for final confirmation".into();
                return Ok(());
            }
            if s.phase != "build" {
                return Err("advance only during build or repair".into());
            }
            let l = s.cursor.layer.clone().ok_or("current layer missing")?;
            if b.components
                .iter()
                .any(|x| s.marks.get(&l).and_then(|m| m.get(x)).is_none())
            {
                return Err("current layer has incomplete components".into());
            }
            let n = b
                .layers
                .iter()
                .position(|x| x == &l)
                .ok_or("invalid layer")?;
            if n + 1 == b.layers.len() {
                if s.issues.iter().any(|i| !i.resolved) {
                    s.phase = "repair".into();
                    s.cursor.next = "resolve open repair issues".into();
                    return Ok(());
                }
                s.phase = "proof".into();
                s.cursor = Cursor {
                    layer: None,
                    incomplete_components: vec![],
                    next: "run a whole check".into(),
                }
            } else {
                let l = b.layers[n + 1].clone();
                let incomplete = b
                    .components
                    .iter()
                    .filter(|c| s.marks.get(&l).and_then(|m| m.get(*c)).is_none())
                    .cloned()
                    .collect::<Vec<_>>();
                s.cursor = Cursor {
                    layer: Some(l),
                    incomplete_components: incomplete,
                    next: b.next.clone(),
                }
            }
            Ok(())
        })
    }
    pub fn issue(&mut self, c: &str, r: String) -> Result<Value, Box<dyn std::error::Error>> {
        bound(&r, MAX, "reason")?;
        let c: String = c.into();
        self.mutate(
            "issue",
            json!({"component":c,"reason":r,"reason_length":r.len()}),
            move |s| {
                component(project(s)?, &c)?;
                if s.phase != "proof" && s.phase != "repair" {
                    return Err("issues only from proof or repair".into());
                }
                if s.issues.iter().any(|x| x.component == c && !x.resolved) {
                    return Err("component already has an open issue".into());
                }
                s.issues.push(Issue {
                    component: c,
                    reason: r,
                    resolved: false,
                });
                s.phase = "repair".into();
                s.proof_generation = None;
                Ok(())
            },
        )
    }
    pub fn changed(&mut self, r: String) -> Result<Value, Box<dyn std::error::Error>> {
        bound(&r, MAX, "reason")?;
        self.mutate(
            "changed",
            json!({"reason":r,"reason_length":r.len()}),
            |s| {
                project(s)?;
                s.generation += 1;
                let ids = s
                    .blueprint
                    .as_ref()
                    .map(|b| b.components.clone())
                    .unwrap_or_default();
                for id in ids {
                    *s.input_epochs.entry(id).or_insert(0) += 1;
                }
                s.proof_generation = None;
                if s.phase == "complete" {
                    s.phase = "proof".into();
                    s.cursor.next = "run a whole check after the declared change".into()
                }
                Ok(())
            },
        )
    }
    pub fn changed_component(
        &mut self,
        id: &str,
        reason: String,
    ) -> Result<Value, Box<dyn std::error::Error>> {
        bound(&reason, MAX, "reason")?;
        let id = id.to_owned();
        self.mutate(
            "changed_component",
            json!({"component":id,"reason":reason}),
            move |s| {
                let b = project(s)?.clone();
                component(&b, &id)?;
                let mut affected = vec![id.clone()];
                for child in dependent_closure(&id, &b, &b) {
                    if !affected.contains(&child) {
                        affected.push(child);
                    }
                }
                s.generation += 1;
                s.proof_generation = None;
                for c in &affected {
                    *s.input_epochs.entry(c.clone()).or_insert(0) += 1;
                }
                if s.phase == "build" {
                    let current_layer = s.cursor.layer.clone().ok_or("current layer missing")?;
                    let from = b
                        .layers
                        .iter()
                        .position(|layer| layer == &current_layer)
                        .ok_or("invalid current layer")?;
                    for c in &affected {
                        for layer in b.layers.iter().skip(from) {
                            if let Some(marks) = s.marks.get_mut(layer) {
                                marks.remove(c);
                            }
                        }
                    }
                    s.cursor.incomplete_components = b
                        .components
                        .iter()
                        .filter(|c| {
                            s.marks
                                .get(&current_layer)
                                .and_then(|marks| marks.get(*c))
                                .is_none()
                        })
                        .cloned()
                        .collect();
                } else {
                    for c in &affected {
                        if let Some(marks) = s.marks.get_mut("repair") {
                            marks.remove(c);
                        }
                        if let Some(issue) = s
                            .issues
                            .iter_mut()
                            .find(|issue| issue.component == *c && !issue.resolved)
                        {
                            issue.reason = reason.clone();
                        } else {
                            s.issues.push(Issue {
                                component: c.clone(),
                                reason: reason.clone(),
                                resolved: false,
                            });
                        }
                    }
                    s.phase = "repair".into();
                    s.cursor = Cursor {
                        layer: None,
                        incomplete_components: vec![],
                        next: "resolve open repair issues".into(),
                    };
                }
                Ok(())
            },
        )
    }
    pub fn finish(&mut self) -> Result<Value, Box<dyn std::error::Error>> {
        self.mutate("finish", json!({}), |s| {
            if s.phase != "proof" || s.proof_generation != Some(s.generation) {
                return Err(
                    "finish requires a successful whole check at current generation".into(),
                );
            }
            s.phase = "complete".into();
            Ok(())
        })
    }
    pub fn reset(&mut self, r: String) -> Result<Value, Box<dyn std::error::Error>> {
        bound(&r, MAX, "reset reason")?;
        let empty = self.empty();
        let tx = self
            .c
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let raw: Option<String> = tx
            .query_row(
                "SELECT document FROM sessions WHERE id=?",
                [&self.id],
                |x| x.get(0),
            )
            .optional()?;
        let old: Session = match raw {
            Some(x) => serde_json::from_str(&x)?,
            None => empty,
        };
        if let Some(expected) = self.expected_revision {
            if old.revision != expected {
                return Err("stale revision".into());
            }
        }
        let fresh = Session {
            schema: SCHEMA,
            session_id: self.id.clone(),
            phase: "blueprint".into(),
            generation: old.generation + 1,
            blueprint: None,
            cursor: Cursor {
                layer: None,
                incomplete_components: vec![],
                next: "plan a blueprint".into(),
            },
            marks: BTreeMap::new(),
            issues: vec![],
            receipts: vec![],
            proof_generation: None,
            revision: old.revision.saturating_add(1),
            input_epochs: BTreeMap::new(),
        };
        tx.execute("INSERT INTO sessions(id,document)VALUES(?,?) ON CONFLICT(id)DO UPDATE SET document=excluded.document",params![self.id,serde_json::to_string(&fresh)?])?;
        tx.execute(
            "INSERT INTO events(session_id,at,kind,data)VALUES(?,?,?,?)",
            params![
                self.id,
                now(),
                "reset_archive",
                serde_json::to_string(&json!({"reason":r,"old":view(&old)}))?
            ],
        )?;
        let path = publish_tx(&tx, &self.id, &self.data, &fresh)?;
        tx.commit()?;
        Ok(with_path(view(&fresh), path))
    }
    pub fn check(&mut self, a: &[String]) -> Result<Value, Box<dyn std::error::Error>> {
        let sep = a
            .iter()
            .position(|x| x == "--")
            .ok_or("check requires --")?;
        if sep < 2 {
            return Err("check requires KIND LABEL".into());
        }
        let (kind, label) = (&a[0], &a[1]);
        bound(label, 256, "label")?;
        let argv = &a[sep + 1..];
        if argv.is_empty() {
            return Err("command required".into());
        }
        let mut why = None;
        let mut scoped: Option<String> = None;
        let mut n = 2;
        while n < sep {
            if a[n] == "--component" {
                n += 1;
                scoped = Some(a.get(n).ok_or("--component requires ID")?.clone());
                n += 1;
                continue;
            }
            if a[n] != "--reason" {
                return Err("unknown check option".into());
            }
            n += 1;
            why = Some(a.get(n).ok_or("--reason requires text")?.clone());
            n += 1
        }
        if kind == "safety" {
            bound(why.as_deref().unwrap_or(""), MAX, "safety reason")?
        }
        if !["smoke", "whole", "narrow", "safety"].contains(&kind.as_str()) {
            return Err("invalid check kind".into());
        }
        if kind == "whole" && scoped.is_some() {
            return Err("whole checks cannot be scoped".into());
        }
        if kind == "smoke" && crate::hooks::recognized_argv(argv) {
            return Err("recognized test command requires whole or narrow check kind".into());
        }
        let hash = hex(&Sha256::digest(serde_json::to_vec(argv)?));
        let cwd = std::env::current_dir()?
            .canonicalize()?
            .to_string_lossy()
            .into_owned();
        let current = self.status()?;
        let generation = current["generation"].as_u64().unwrap_or(0);
        let epoch = scoped
            .as_ref()
            .and_then(|c| current.pointer("/input_epochs")?.get(c))
            .and_then(Value::as_u64)
            .unwrap_or(0);
        if current["blueprint"].is_null() {
            return Err("plan a blueprint first".into());
        }
        if let Some(component_id) = scoped.as_deref() {
            if !current
                .pointer("/blueprint/components")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .any(|entry| entry.as_str() == Some(component_id))
            {
                return Err("unknown component".into());
            }
        }
        match kind.as_str() {
            "smoke" if current["phase"] != "build" => return Err("smoke only during build".into()),
            "whole" if current["phase"] != "proof" => return Err("whole only in proof".into()),
            "narrow"
                if current["phase"] != "repair"
                    || !current["issues"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .any(|i| i["resolved"] == false) =>
            {
                return Err("narrow needs an open repair issue".into())
            }
            _ => {}
        }
        if let Some(receipt) = current["receipts"]
            .as_array()
            .into_iter()
            .flatten()
            .rev()
            .find(|x| {
                x["kind"].as_str() == Some(kind.as_str())
                    && x["argv_sha256"].as_str() == Some(hash.as_str())
                    && x["component"].as_str() == scoped.as_deref()
                    && x["cwd"].as_str() == Some(cwd.as_str())
                    && x["status"] == "success"
                    && if scoped.is_some() {
                        x["component_epoch"].as_u64().unwrap_or(0) == epoch
                    } else {
                        x["generation"].as_u64() == Some(generation)
                    }
            })
        {
            return Ok(
                json!({"success":true,"reused":true,"receipt":receipt,"generation":generation,"kind":kind,"label":label,"cursor":current["cursor"]}),
            );
        }
        let r = self.mutate(
            "check_reserved",
            json!({"kind":kind,"label":label,"argv_sha256":hash,"reason":why}),
            |s| {
                project(s)?;
                if let Some(c) = &scoped {
                    component(s.blueprint.as_ref().unwrap(), c)?;
                }
                let epoch = scoped
                    .as_ref()
                    .map(|c| *s.input_epochs.get(c).unwrap_or(&0))
                    .unwrap_or(0);
                match kind.as_str() {
                    "smoke" if s.phase != "build" => return Err("smoke only during build".into()),
                    "whole" if s.phase != "proof" => return Err("whole only in proof".into()),
                    "narrow" if s.phase != "repair" || !s.issues.iter().any(|i| !i.resolved) => {
                        return Err("narrow needs an open repair issue".into())
                    }
                    _ => {}
                }
                if s.receipts.iter().any(|x| {
                    x.kind == *kind
                        && x.argv_sha256 == hash
                        && x.component == scoped
                        && x.cwd.as_deref() == Some(cwd.as_str())
                        && ((scoped.is_some() && x.component_epoch == epoch)
                            || (scoped.is_none() && x.generation == s.generation))
                }) {
                    return Err("same check already reserved at this generation".into());
                }
                s.receipts.push(Receipt {
                    kind: kind.clone(),
                    label: label.clone(),
                    argv_sha256: hash.clone(),
                    generation: s.generation,
                    status: "running".into(),
                    exit_code: None,
                    component: scoped.clone(),
                    component_epoch: epoch,
                    cwd: Some(cwd.clone()),
                });
                Ok(())
            },
        )?;
        let g = r["generation"].as_u64().unwrap();
        let epoch = scoped
            .as_ref()
            .map(|c| {
                r.pointer("/input_epochs")
                    .and_then(|x| x.get(c))
                    .and_then(Value::as_u64)
                    .unwrap_or(0)
            })
            .unwrap_or(0);
        let mut command = Command::new(&argv[0]);
        command.args(&argv[1..]).current_dir(&cwd);
        crate::environment::clean_command(&mut command);
        let z = command.status();
        let (ok, code, msg) = match z {
            Ok(x) => (x.success(), x.code().unwrap_or(1), None),
            Err(e) => (false, 127, Some(e.to_string())),
        };
        self.mutate(
            "check_finished",
            json!({"kind":kind,"label":label,"argv_sha256":hash,"success":ok,"exit_code":code}),
            |s| {
                let q = s
                    .receipts
                    .iter_mut()
                    .find(|x| {
                        x.generation == g
                            && x.kind == *kind
                            && x.argv_sha256 == hash
                            && x.component == scoped
                            && x.cwd.as_deref() == Some(cwd.as_str())
                            && x.component_epoch == epoch
                            && x.status == "running"
                    })
                    .ok_or("running receipt lost")?;
                q.status = if ok {
                    "success".into()
                } else {
                    "failed".into()
                };
                q.exit_code = Some(code);
                if kind == "whole" {
                    if s.generation == g {
                        s.proof_generation = if ok { Some(g) } else { None };
                    }
                }
                Ok(())
            },
        )?;
        if !ok {
            return Err(msg
                .unwrap_or_else(|| format!("check failed with exit code {code}"))
                .into());
        }
        Ok(
            json!({"success":true,"generation":g,"kind":kind,"label":label,"cursor":self.status()?["cursor"]}),
        )
    }
    pub fn doctor(&self) -> Result<(), Box<dyn std::error::Error>> {
        let _: i64 = self
            .c
            .query_row("SELECT version FROM graphfather_schema", [], |r| r.get(0))?;
        println!("state      ok\nschema     1\nwritable   ok");
        Ok(())
    }
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn publish_tx(
    c: &rusqlite::Transaction<'_>,
    id: &str,
    data: &Path,
    s: &Session,
) -> Result<std::path::PathBuf, Box<dyn std::error::Error>> {
    let mut q = c.prepare(
        "SELECT data FROM events WHERE session_id=? AND kind='revise' ORDER BY seq DESC LIMIT 8",
    )?;
    let rows = q.query_map([id], |r| r.get::<_, String>(0))?;
    let mut h = Vec::new();
    for x in rows {
        let v: Value = serde_json::from_str(&x?)?;
        let old = v
            .get("old")
            .and_then(Value::as_str)
            .and_then(|x| serde_json::from_str::<Value>(x).ok())
            .and_then(|x| x.get("revision").and_then(Value::as_u64))
            .unwrap_or(0);
        let reason = v
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        h.push((old, reason));
    }
    Ok(crate::markdown::publish(data, &view(s), &h)?)
}
fn with_path(mut v: Value, p: std::path::PathBuf) -> Value {
    v["plan_path"] = json!(p);
    v
}
fn hex(x: &[u8]) -> String {
    x.iter().map(|x| format!("{x:02x}")).collect()
}
fn observation_input_hash(
    tool_name: &str,
    tool_input: &Value,
    cwd: &str,
) -> Result<String, Box<dyn std::error::Error>> {
    Ok(hex(&Sha256::digest(serde_json::to_vec(&json!({
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": cwd
    }))?)))
}
fn observation_view(
    id: i64,
    session_id: &str,
    generation: u64,
    cwd: &str,
    tool_name: &str,
    input_sha256: &str,
    outcome_sha256: &str,
    status: &str,
) -> Value {
    json!({"kind":"observation","receipt_id":format!("obs-{id}"),"session_id":session_id,"generation":generation,"cwd":cwd,"tool_name":tool_name,"input_sha256":input_sha256,"outcome_sha256":outcome_sha256,"status":status})
}
fn bound(x: &str, n: usize, k: &str) -> Result<(), Box<dyn std::error::Error>> {
    if x.trim().is_empty() || x.len() > n {
        Err(format!("{k} must be nonempty and at most {n} bytes").into())
    } else {
        Ok(())
    }
}
fn valid(b: &Blueprint) -> Result<(), Box<dyn std::error::Error>> {
    bound(&b.objective, MAX, "objective")?;
    bound(&b.next, MAX, "next")?;
    if b.components.is_empty()
        || b.components.len() > 64
        || b.layers.is_empty()
        || b.layers.len() > 16
        || b.layers[0] != "scaffold"
    {
        return Err("blueprint requires scaffold-first, 1-64 components, and 1-16 layers".into());
    }
    for v in [&b.components, &b.layers] {
        let mut h = HashSet::new();
        for x in v {
            bound(x, 256, "blueprint id")?;
            if !h.insert(x) {
                return Err("component/layer ids must be unique".into());
            }
        }
    }
    Ok(())
}
fn validate_dependencies(b: &Blueprint) -> Result<(), Box<dyn std::error::Error>> {
    for (c, ps) in &b.dependencies {
        component(b, c)?;
        let mut seen = HashSet::new();
        for p in ps {
            component(b, p)?;
            if p == c || !seen.insert(p) {
                return Err("invalid dependency edge".into());
            }
        }
    }
    fn visit(
        x: &str,
        b: &Blueprint,
        stack: &mut HashSet<String>,
        done: &mut HashSet<String>,
    ) -> bool {
        if done.contains(x) {
            return false;
        }
        if !stack.insert(x.into()) {
            return true;
        }
        for p in b.dependencies.get(x).into_iter().flatten() {
            if visit(p, b, stack, done) {
                return true;
            }
        }
        stack.remove(x);
        done.insert(x.into());
        false
    }
    let mut st = HashSet::new();
    let mut d = HashSet::new();
    for c in &b.components {
        if visit(c, b, &mut st, &mut d) {
            return Err("dependency cycle".into());
        }
    }
    Ok(())
}
fn project(s: &Session) -> Result<&Blueprint, Box<dyn std::error::Error>> {
    s.blueprint
        .as_ref()
        .ok_or_else(|| "plan a blueprint first".into())
}
fn component(b: &Blueprint, c: &str) -> Result<(), Box<dyn std::error::Error>> {
    if b.components.iter().any(|x| x == c) {
        Ok(())
    } else {
        Err("unknown component".into())
    }
}
fn dependent_closure(seed: &str, old: &Blueprint, new: &Blueprint) -> Vec<String> {
    let mut out = Vec::new();
    let mut todo = vec![seed.to_string()];
    while let Some(x) = todo.pop() {
        for b in [old, new] {
            for (c, ds) in &b.dependencies {
                if ds.iter().any(|d| d == &x) && !out.contains(c) {
                    out.push(c.clone());
                    todo.push(c.clone());
                }
            }
        }
    }
    out
}
fn view(s: &Session) -> Value {
    json!({"schema":s.schema,"session_id":s.session_id,"phase":s.phase,"generation":s.generation,"revision":s.revision,"input_epochs":s.input_epochs,"blueprint":s.blueprint,"cursor":s.cursor,"marks":s.marks,"issues":s.issues,"receipts":s.receipts,"proof_generation":s.proof_generation})
}
#[cfg(unix)]
fn perm(p: &Path, m: u32) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(p, fs::Permissions::from_mode(m))
}
#[cfg(not(unix))]
fn perm(_: &Path, _: u32) -> std::io::Result<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Mutex, OnceLock};

    static CWD_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

    #[test]
    fn compact_empty_state_does_not_create_a_plan() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(dir.path(), "empty-task").unwrap();
        let compact = store.compact_status().unwrap();
        assert_eq!(compact["schema"], "ctrlissues.cursor.v1");
        assert_eq!(compact["objective"], "");
        assert!(compact["plan_path"].is_null());
        assert!(!dir.path().join("plans").exists());
        assert!(compact.get("blueprint").is_none());
    }

    #[test]
    fn check_receipt_reuse_requires_same_cwd_and_success() {
        let _guard = CWD_LOCK.get_or_init(|| Mutex::new(())).lock().unwrap();
        let original = std::env::current_dir().unwrap();
        let root = tempfile::tempdir().unwrap();
        let cwd_a = root.path().join("a");
        let cwd_b = root.path().join("b");
        fs::create_dir_all(&cwd_a).unwrap();
        fs::create_dir_all(&cwd_b).unwrap();
        let data = root.path().join("data");
        fs::create_dir_all(&data).unwrap();
        let blueprint_path = root.path().join("blueprint.json");
        fs::write(&blueprint_path, r#"{"objective":"receipt test","components":["core"],"layers":["scaffold"],"next":"check"}"#).unwrap();
        let mut store = Store::open(&data, "receipt-task").unwrap();
        store.plan(blueprint_path.to_str().unwrap()).unwrap();
        std::env::set_current_dir(&cwd_a).unwrap();
        let mut args = vec!["smoke".into(), "portable-check".into(), "--".into()];
        args.extend(exit_command(0));
        let first = store.check(&args).unwrap();
        assert_eq!(first["reused"], Value::Null);
        let replay = store.check(&args).unwrap();
        assert_eq!(replay["reused"], true);
        let raw: String = store
            .c
            .query_row(
                "SELECT document FROM sessions WHERE id='receipt-task'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let mut legacy: Value = serde_json::from_str(&raw).unwrap();
        for receipt in legacy["receipts"].as_array_mut().unwrap() {
            receipt.as_object_mut().unwrap().remove("cwd");
        }
        store
            .c
            .execute(
                "UPDATE sessions SET document=? WHERE id='receipt-task'",
                [legacy.to_string()],
            )
            .unwrap();
        let migrated = store.check(&args).unwrap();
        assert_eq!(migrated["reused"], Value::Null);
        std::env::set_current_dir(&cwd_b).unwrap();
        let other_dir = store.check(&args).unwrap();
        assert_eq!(other_dir["reused"], Value::Null);
        let mut failed = vec!["smoke".into(), "failure".into(), "--".into()];
        failed.extend(exit_command(7));
        assert!(store.check(&failed).is_err());
        assert!(store.check(&failed).is_err());
        std::env::set_current_dir(original).unwrap();
    }

    #[test]
    fn unknown_scoped_component_cannot_reuse_a_success_receipt() {
        let dir = tempfile::tempdir().unwrap();
        let blueprint = dir.path().join("blueprint.json");
        fs::write(&blueprint, r#"{"objective":"scoped receipt","components":["core"],"layers":["scaffold"],"next":"check"}"#).unwrap();
        let mut store = Store::open(dir.path(), "scoped-receipt-task").unwrap();
        store.plan(blueprint.to_str().unwrap()).unwrap();
        let mut args = vec![
            "smoke".into(),
            "core-check".into(),
            "--component".into(),
            "core".into(),
            "--".into(),
        ];
        args.extend(exit_command(0));
        assert_eq!(store.check(&args).unwrap()["success"], true);
        let raw: String = store
            .c
            .query_row(
                "SELECT document FROM sessions WHERE id='scoped-receipt-task'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let mut document: Value = serde_json::from_str(&raw).unwrap();
        document["blueprint"]["components"] = json!(["replacement"]);
        store
            .c
            .execute(
                "UPDATE sessions SET document=? WHERE id='scoped-receipt-task'",
                [document.to_string()],
            )
            .unwrap();
        assert!(store.check(&args).is_err());
    }

    #[test]
    fn scoped_change_preserves_build_work_and_routes_proof_changes_to_repair() {
        let dir = tempfile::tempdir().unwrap();
        let blueprint = dir.path().join("blueprint.json");
        fs::write(&blueprint, r#"{"objective":"scope test","components":["a","b"],"layers":["scaffold","implementation"],"next":"blueprint next","dependencies":{}}"#).unwrap();
        let mut store = Store::open(dir.path(), "scope-task").unwrap();
        store.plan(blueprint.to_str().unwrap()).unwrap();
        store.mark("a", "scaffold a").unwrap();
        store.mark("b", "scaffold b").unwrap();
        store.advance().unwrap();
        store.cursor("preserve this next action".into()).unwrap();
        store.mark("a", "implementation a").unwrap();
        store
            .changed_component("a", "source changed".into())
            .unwrap();
        let status = store.status().unwrap();
        assert_eq!(status["generation"], 1);
        assert_eq!(status["marks"]["scaffold"]["a"], "scaffold a");
        assert_eq!(status["marks"]["scaffold"]["b"], "scaffold b");
        assert!(status["marks"]["implementation"].get("a").is_none());
        assert_eq!(status.pointer("/input_epochs/a"), Some(&json!(1)));
        assert_eq!(status["cursor"]["next"], "preserve this next action");
        assert_eq!(status["cursor"]["incomplete_components"], json!(["a", "b"]));

        store.mark("a", "implementation a").unwrap();
        store.mark("b", "implementation b").unwrap();
        store.advance().unwrap();
        assert_eq!(store.status().unwrap()["phase"], "proof");
        store
            .changed_component("a", "proof input changed".into())
            .unwrap();
        let proof_change = store.status().unwrap();
        assert_eq!(proof_change["phase"], "repair");
        assert_eq!(proof_change["marks"]["scaffold"]["a"], "scaffold a");
        assert_eq!(
            proof_change["marks"]["implementation"]["a"],
            "implementation a"
        );
        assert_eq!(proof_change["issues"][0]["reason"], "proof input changed");
        store
            .changed_component("a", "refreshed proof issue".into())
            .unwrap();
        let refreshed = store.status().unwrap();
        assert_eq!(refreshed["issues"].as_array().unwrap().len(), 1);
        assert_eq!(refreshed["issues"][0]["reason"], "refreshed proof issue");
    }

    #[test]
    fn observation_receipts_are_task_generation_bound_and_metadata_only() {
        let dir = tempfile::tempdir().unwrap();
        let blueprint = dir.path().join("blueprint.json");
        fs::write(&blueprint, r#"{"objective":"observation test","components":["core"],"layers":["scaffold"],"next":"check"}"#).unwrap();
        let mut store = Store::open(dir.path(), "observation-task").unwrap();
        store.plan(blueprint.to_str().unwrap()).unwrap();
        let input = json!({"path":"src/main.rs"});
        let outcome = json!({"success":true,"output":"private observation output"});
        let receipt = store
            .record_observation("read_file", &input, "/work", &outcome, true)
            .unwrap()
            .unwrap();
        assert_eq!(receipt["status"], "success");
        assert_eq!(
            store
                .completed_observation("read_file", &input, "/work")
                .unwrap()
                .unwrap()["receipt_id"],
            receipt["receipt_id"]
        );
        assert!(receipt
            .to_string()
            .find("private observation output")
            .is_none());
        assert_eq!(
            store.compact_status().unwrap()["recent_receipts"][0]["kind"],
            "observation"
        );
        store.changed("declared input changed".into()).unwrap();
        assert!(store
            .completed_observation("read_file", &input, "/work")
            .unwrap()
            .is_none());
    }

    #[cfg(windows)]
    fn exit_command(code: i32) -> Vec<String> {
        vec!["cmd".into(), "/C".into(), format!("exit {code}")]
    }

    #[cfg(not(windows))]
    fn exit_command(code: i32) -> Vec<String> {
        vec!["sh".into(), "-c".into(), format!("exit {code}")]
    }
}
