//! Process-owned, bounded-cardinality instrumentation. No collectors exist when disabled.
use anyhow::{bail, Result};
use prometheus::{
    Encoder, HistogramOpts, HistogramVec, IntCounterVec, IntGaugeVec, Opts, Registry, TextEncoder,
};
use std::{
    collections::HashMap,
    sync::Arc,
    time::{Instant, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    task::JoinSet,
    time::{timeout, Duration},
};

pub const BUCKETS: &[f64] = &[
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1., 2.5, 5., 10., 30., 60., 120., 300., 600.,
];
const OUTCOMES: &[&str] = &["ok", "error", "aborted"];
pub const KINDS: &[&str] = &[
    "read",
    "write",
    "transaction",
    "stateful",
    "unsupported",
    "malformed",
];
const QUERY_OUTCOMES: &[&str] = &[
    "ok",
    "sql_error",
    "router_error",
    "delivery_error",
    "aborted",
];
pub const EXTENDED_KINDS: &[&str] = &[
    "parse",
    "bind",
    "describe",
    "execute",
    "close",
    "flush",
    "sync",
    "malformed",
];
const ROLES: &[&str] = &["primary", "replica", "unavailable"];
#[derive(Clone, Default)]
pub struct Metrics(Option<Arc<Inner>>);
struct Inner {
    registry: Registry,
    counters: HashMap<&'static str, IntCounterVec>,
    gauges: HashMap<&'static str, IntGaugeVec>,
    histograms: HashMap<&'static str, HistogramVec>,
}
impl Metrics {
    pub fn new(
        enabled: bool,
        nodes: &[super::Node],
        routing: bool,
        capacity: usize,
    ) -> Result<Self> {
        if !enabled {
            return Ok(Self::default());
        }
        let mut inner = Inner {
            registry: Registry::new(),
            counters: HashMap::new(),
            gauges: HashMap::new(),
            histograms: HashMap::new(),
        };
        for (name, labels) in [
            ("clients_accepted_total", &[][..]),
            ("clients_rejected_total", &[][..]),
            ("sessions_completed_total", &["outcome"][..]),
            ("backend_connects_total", &["node", "outcome"][..]),
            ("extended_frames_total", &["kind", "outcome"][..]),
            ("extended_cycles_total", &["outcome"][..]),
            ("extended_backend_exchanges_total", &["outcome"][..]),
            ("queries_total", &["kind", "outcome"][..]),
            ("query_attempts_total", &["route", "outcome"][..]),
            ("read_retries_total", &[][..]),
            ("control_requests_total", &[][..]),
            ("pool_reuses_total", &[][..]),
            ("pool_discards_total", &[][..]),
            ("pool_resets_total", &["outcome"][..]),
            ("pool_waits_total", &["outcome"][..]),
            ("result_buffer_rejections_total", &[][..]),
            ("topology_changes_total", &[][..]),
            ("primary_changes_total", &[][..]),
            ("probes_total", &["node", "outcome"][..]),
        ] {
            let v = IntCounterVec::new(Opts::new(format!("rustybouncer_{name}"), name), labels)?;
            inner.registry.register(Box::new(v.clone()))?;
            inner.counters.insert(name, v);
        }
        for (name, labels) in [
            ("routing_enabled", &[][..]),
            ("pooling_enabled", &[][..]),
            ("pooling_mode", &["mode"][..]),
            ("pool_backend_capacity", &[][..]),
            ("pool_size", &[][..]),
            ("pool_reserve_size", &[][..]),
            ("pool_backend_reservations", &[][..]),
            ("pool_backends", &["phase"][..]),
            ("pool_waiters", &[][..]),
            ("result_buffer_limit_bytes", &[][..]),
            ("result_buffer_reserved_bytes", &[][..]),
            ("result_buffer_highwater_bytes", &[][..]),
            ("result_buffer_allocated_bytes", &[][..]),
            ("build_info", &["version"][..]),
            ("clients_active", &[][..]),
            ("client_capacity", &[][..]),
            ("backend_connections_active", &["node"][..]),
            ("extended_frames_in_flight", &[][..]),
            ("extended_cycles_in_flight", &[][..]),
            ("queries_in_flight", &[][..]),
            ("topology_nodes", &["role"][..]),
            ("node_role", &["node", "role"][..]),
            ("topology_ambiguous", &[][..]),
            ("topology_discovery_completed", &[][..]),
            ("topology_last_completed_timestamp_seconds", &[][..]),
        ] {
            let v = IntGaugeVec::new(Opts::new(format!("rustybouncer_{name}"), name), labels)?;
            inner.registry.register(Box::new(v.clone()))?;
            inner.gauges.insert(name, v);
        }
        for (name, labels) in [
            ("session_duration_seconds", &[][..]),
            ("pool_reset_duration_seconds", &[][..]),
            ("pool_wait_duration_seconds", &[][..]),
            ("extended_frame_duration_seconds", &[][..]),
            ("extended_cycle_duration_seconds", &[][..]),
            ("query_duration_seconds", &[][..]),
            ("route_wait_duration_seconds", &[][..]),
            ("probe_duration_seconds", &["node"][..]),
        ] {
            let v = HistogramVec::new(
                HistogramOpts::new(format!("rustybouncer_{name}"), name).buckets(BUCKETS.to_vec()),
                labels,
            )?;
            inner.registry.register(Box::new(v.clone()))?;
            inner.histograms.insert(name, v);
        }
        let m = Self(Some(Arc::new(inner)));
        m.initialize(nodes);
        m.set("routing_enabled", &[], i64::from(routing));
        m.set("client_capacity", &[], capacity as i64);
        m.set("build_info", &[env!("CARGO_PKG_VERSION")], 1);
        m.configure_pool("none", 0, 0, 0);
        Ok(m)
    }
    fn initialize(&self, nodes: &[super::Node]) {
        let i = self.0.as_ref().unwrap();
        for v in i.counters.values() {
            if v.desc()[0].variable_labels.is_empty() {
                v.with_label_values(&[] as &[&str]);
            }
        }
        for v in i.gauges.values() {
            if v.desc()[0].variable_labels.is_empty() {
                v.with_label_values(&[] as &[&str]);
            }
        }
        for v in i.histograms.values() {
            if v.desc()[0].variable_labels.is_empty() {
                v.with_label_values(&[] as &[&str]);
            }
        }
        for outcome in OUTCOMES {
            self.counter("pool_resets_total", &[*outcome]);
            self.counter("pool_waits_total", &[*outcome]);
            self.counter("sessions_completed_total", &[*outcome]);
        }
        for phase in ["connecting", "leased", "resetting", "idle"] {
            self.set("pool_backends", &[phase], 0);
        }
        for kind in KINDS {
            for outcome in QUERY_OUTCOMES {
                self.counter("queries_total", &[*kind, *outcome]);
            }
        }
        for outcome in QUERY_OUTCOMES {
            self.counter("extended_cycles_total", &[*outcome]);
            for kind in EXTENDED_KINDS {
                self.counter("extended_frames_total", &[*kind, *outcome]);
            }
        }
        for outcome in ["ok", "sql_error", "error", "aborted"] {
            self.counter("extended_backend_exchanges_total", &[outcome]);
        }
        for route in ["primary", "replica"] {
            for outcome in ["ok", "sql_error", "error", "aborted"] {
                self.counter("query_attempts_total", &[route, outcome]);
            }
        }
        for role in ROLES {
            self.set("topology_nodes", &[*role], 0);
        }
        for node in nodes {
            self.set("backend_connections_active", &[&node.name], 0);
            for outcome in OUTCOMES {
                self.counter("backend_connects_total", &[&node.name, *outcome]);
            }
            for outcome in ["ok", "error"] {
                self.counter("probes_total", &[&node.name, outcome]);
            }
            for role in ROLES {
                self.set("node_role", &[&node.name, *role], 0);
            }
            i.histograms["probe_duration_seconds"].with_label_values(&[&node.name]);
        }
    }
    pub fn configure_pool(&self, mode: &str, capacity: usize, size: usize, reserve: usize) {
        let enabled = mode != "none";
        self.set("pooling_enabled", &[], i64::from(enabled));
        self.set(
            "pool_backend_capacity",
            &[],
            if enabled { capacity as i64 } else { 0 },
        );
        self.set("pool_size", &[], if enabled { size as i64 } else { 0 });
        self.set(
            "pool_reserve_size",
            &[],
            if enabled { reserve as i64 } else { 0 },
        );
        for value in ["none", "session", "transaction"] {
            self.set("pooling_mode", &[value], i64::from(mode == value));
        }
    }
    fn counter(&self, name: &str, labels: &[&str]) {
        if let Some(i) = &self.0 {
            i.counters[name].with_label_values(labels);
        }
    }
    pub fn inc(&self, name: &str, labels: &[&str]) {
        if let Some(i) = &self.0 {
            i.counters[name].with_label_values(labels).inc();
        }
    }
    pub fn set(&self, name: &str, labels: &[&str], value: i64) {
        if let Some(i) = &self.0 {
            i.gauges[name].with_label_values(labels).set(value);
        }
    }
    pub(crate) fn add(&self, name: &str, labels: &[&str], value: i64) {
        if let Some(i) = &self.0 {
            i.gauges[name].with_label_values(labels).add(value);
        }
    }
    pub fn observe(&self, name: &str, labels: &[&str], elapsed: f64) {
        if let Some(i) = &self.0 {
            i.histograms[name]
                .with_label_values(labels)
                .observe(elapsed);
        }
    }
    pub fn render(&self) -> Result<Vec<u8>> {
        let mut output = Vec::new();
        if let Some(i) = &self.0 {
            TextEncoder::new().encode(&i.registry.gather(), &mut output)?;
        }
        Ok(output)
    }
    pub fn topology(
        &self,
        nodes: &[super::Node],
        next: &super::Topology,
        changed: bool,
        primary_changed: bool,
    ) {
        let mut counts = [0; 3];
        for (index, node) in nodes.iter().enumerate() {
            let role = if next.primary == Some(index) {
                0
            } else if next.replicas.contains(&index) {
                1
            } else {
                2
            };
            counts[role] += 1;
            for (r, label) in ROLES.iter().enumerate() {
                self.set("node_role", &[&node.name, label], i64::from(role == r));
            }
        }
        for (role, count) in ROLES.iter().zip(counts) {
            self.set("topology_nodes", &[*role], count);
        }
        self.set("topology_ambiguous", &[], i64::from(next.ambiguous));
        self.set("topology_discovery_completed", &[], 1);
        self.set(
            "topology_last_completed_timestamp_seconds",
            &[],
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs() as i64,
        );
        if changed {
            self.inc("topology_changes_total", &[]);
        }
        if primary_changed {
            self.inc("primary_changes_total", &[]);
        }
    }
    pub fn guard(&self, event: Event) -> Guard {
        if self.0.is_none() {
            return Guard(None);
        }
        let live = match &event {
            Event::Session => Some(("clients_active", vec![])),
            Event::ExtendedFrame(_) => Some(("extended_frames_in_flight", vec![])),
            Event::ExtendedCycle => Some(("extended_cycles_in_flight", vec![])),
            Event::Query(_) => Some(("queries_in_flight", vec![])),
            Event::Backend(node) => Some(("backend_connections_active", vec![node.as_str()])),
            _ => None,
        };
        if let Some((name, labels)) = live {
            self.add(name, &labels, 1);
        }
        Guard(Some(Record {
            metrics: self.clone(),
            event,
            start: Instant::now(),
            outcome: Outcome::Aborted,
        }))
    }
    pub fn timer(&self, name: &'static str) -> Timer {
        Timer(
            self.0
                .as_ref()
                .map(|_| (self.clone(), name, Instant::now())),
        )
    }
}
use prometheus::core::Collector;
#[derive(Clone, Copy)]
pub enum Outcome {
    Ok,
    Error,
    Aborted,
    SqlError,
    RouterError,
    DeliveryError,
}
impl Outcome {
    fn label(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Error => "error",
            Self::Aborted => "aborted",
            Self::SqlError => "sql_error",
            Self::RouterError => "router_error",
            Self::DeliveryError => "delivery_error",
        }
    }
}
pub enum Event {
    Session,
    PoolReset,
    PoolWait,
    ExtendedFrame(&'static str),
    ExtendedCycle,
    ExtendedExchange,
    Query(&'static str),
    Attempt(&'static str),
    Connect(String),
    Backend(String),
}
struct Record {
    metrics: Metrics,
    event: Event,
    start: Instant,
    outcome: Outcome,
}
pub struct Guard(Option<Record>);
impl Guard {
    /// Classification is synchronous, but its cost belongs to query latency.
    pub fn classify_query(&mut self, kind: &'static str) {
        if let Some(Record {
            event: Event::Query(label),
            ..
        }) = &mut self.0
        {
            *label = kind;
        }
    }
    pub fn finish_if_unfinished(&mut self, outcome: Outcome) {
        if let Some(r) = &mut self.0 {
            if matches!(r.outcome, Outcome::Aborted) {
                r.outcome = outcome;
            }
        }
    }
    pub fn finish(&mut self, outcome: Outcome) {
        if let Some(r) = &mut self.0 {
            r.outcome = outcome;
        }
    }
}
impl Drop for Guard {
    fn drop(&mut self) {
        let Some(r) = self.0.take() else {
            return;
        };
        let m = &r.metrics;
        let outcome = r.outcome.label();
        let duration = r.start.elapsed().as_secs_f64();
        match r.event {
            Event::ExtendedFrame(kind) => {
                m.add("extended_frames_in_flight", &[], -1);
                m.inc("extended_frames_total", &[kind, outcome]);
                m.observe("extended_frame_duration_seconds", &[], duration);
            }
            Event::ExtendedCycle => {
                m.add("extended_cycles_in_flight", &[], -1);
                m.inc("extended_cycles_total", &[outcome]);
                m.observe("extended_cycle_duration_seconds", &[], duration);
            }
            Event::ExtendedExchange => m.inc("extended_backend_exchanges_total", &[outcome]),
            Event::PoolWait => {
                m.inc("pool_waits_total", &[outcome]);
                m.observe("pool_wait_duration_seconds", &[], duration);
            }
            Event::PoolReset => {
                m.inc("pool_resets_total", &[outcome]);
                m.observe("pool_reset_duration_seconds", &[], duration);
            }
            Event::Session => {
                m.add("clients_active", &[], -1);
                m.inc("sessions_completed_total", &[outcome]);
                m.observe("session_duration_seconds", &[], duration);
            }
            Event::Query(kind) => {
                m.add("queries_in_flight", &[], -1);
                m.inc("queries_total", &[kind, outcome]);
                m.observe("query_duration_seconds", &[], duration);
            }
            Event::Attempt(route) => m.inc("query_attempts_total", &[route, outcome]),
            Event::Connect(node) => m.inc("backend_connects_total", &[&node, outcome]),
            Event::Backend(node) => m.add("backend_connections_active", &[&node], -1),
        }
    }
}
pub struct Timer(Option<(Metrics, &'static str, Instant)>);
impl Drop for Timer {
    fn drop(&mut self) {
        if let Some((m, n, t)) = &self.0 {
            m.observe(n, &[], t.elapsed().as_secs_f64());
        }
    }
}

/// Bounded tasks and headers; overload sockets are dropped immediately.
pub async fn serve(listener: TcpListener, metrics: Metrics) -> Result<()> {
    let mut tasks = JoinSet::new();
    loop {
        tokio::select! {
            done = tasks.join_next(), if !tasks.is_empty() => { if let Some(Err(e)) = done { return Err(e.into()); } }
            accepted = listener.accept() => {
                let (stream,_) = accepted?;
                spawn_scrape(&mut tasks, stream, &metrics);
            }
        }
    }
}
fn request(header: &[u8]) -> Result<u16> {
    let text = std::str::from_utf8(header)?;
    let mut lines = text.split("\r\n");
    let parts: Vec<_> = lines.next().unwrap_or("").split(' ').collect();
    if parts.len() != 3 || !matches!(parts[2], "HTTP/1.0" | "HTTP/1.1") {
        bail!("invalid request line");
    }
    for line in lines.take_while(|line| !line.is_empty()) {
        let (name, value) = line
            .split_once(':')
            .ok_or_else(|| anyhow::anyhow!("invalid header"))?;
        if name.is_empty()
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b))
            || value.bytes().any(|b| b < 32 && b != b'\t')
        {
            bail!("invalid header");
        }
    }
    Ok(if parts[0] != "GET" {
        405
    } else if parts[1] != "/metrics" {
        404
    } else {
        200
    })
}
async fn http(mut stream: TcpStream, metrics: Metrics) -> Result<()> {
    let mut header = Vec::with_capacity(1024);
    let code = loop {
        if header.ends_with(b"\r\n\r\n") {
            break request(&header).unwrap_or(400);
        }
        if header.len() == 8192 {
            break 431;
        }
        let byte = stream.read_u8().await?;
        header.push(byte);
    };
    let body = if code == 200 {
        metrics.render()?
    } else {
        Vec::new()
    };
    let reason = match code {
        200 => "OK",
        404 => "Not Found",
        405 => "Method Not Allowed",
        431 => "Request Header Fields Too Large",
        _ => "Bad Request",
    };
    let allow = if code == 405 { "Allow: GET\r\n" } else { "" };
    stream.write_all(format!("HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain; version=0.0.4; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n{allow}\r\n",body.len()).as_bytes()).await?;
    stream.write_all(&body).await?;
    stream.shutdown().await?;
    Ok(())
}

/// Even a clean return is unexpected: this listener must live with the process.
pub fn listener_exit(
    result: std::result::Result<Result<()>, tokio::task::JoinError>,
) -> Result<()> {
    bail!("metrics listener exited unexpectedly: {result:?}")
}

fn spawn_scrape(tasks: &mut JoinSet<()>, stream: TcpStream, metrics: &Metrics) -> bool {
    if tasks.len() >= 16 {
        return false;
    }
    let m = metrics.clone();
    tasks.spawn(async move {
        let _ = timeout(Duration::from_secs(2), http(stream, m)).await;
    });
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> super::super::Config {
        toml::from_str(include_str!("../rustybouncer.toml")).unwrap()
    }
    fn enabled() -> Metrics {
        let c = config();
        Metrics::new(true, &c.nodes, true, c.max_clients).unwrap()
    }
    fn exposition(m: &Metrics) -> String {
        String::from_utf8(m.render().unwrap()).unwrap()
    }
    #[test]
    fn disabled_is_empty_and_guards_are_noops() {
        let m = Metrics::default();
        let _g = m.guard(Event::Session);
        assert!(m.0.is_none());
        assert!(m.render().unwrap().is_empty());
    }
    #[test]
    fn finite_series_budget_and_escaped_configured_labels() {
        let mut c = config();
        c.nodes[0].name = "quote\"slash\\line\nend".into();
        let m = Metrics::new(true, &c.nodes, false, 100).unwrap();
        let text = exposition(&m);
        assert_eq!(
            text.lines()
                .filter(|l| !l.starts_with('#') && !l.is_empty())
                .count(),
            275 + 29 * c.nodes.len()
        );
        assert!(text.contains("node=\"quote\\\"slash\\\\line\\nend\""));
        assert!(text.contains("rustybouncer_routing_enabled 0\n"));
        assert!(!text.contains("_created"));
        for family in m.0.as_ref().unwrap().registry.gather() {
            for metric in family.get_metric() {
                if family.get_field_type() == prometheus::proto::MetricType::HISTOGRAM {
                    let histogram = metric.get_histogram();
                    let bounds: Vec<_> = histogram
                        .get_bucket()
                        .iter()
                        .map(|b| b.upper_bound())
                        .collect();
                    assert_eq!(bounds, BUCKETS);
                    assert_eq!(histogram.get_sample_count(), 0);
                }
            }
        }
    }
    #[tokio::test]
    async fn abort_reconciles_session_query_and_backend_lifetimes() {
        let m = enabled();
        let clone = m.clone();
        let (ready, rx) = tokio::sync::oneshot::channel();
        let task = tokio::spawn(async move {
            let _session = clone.guard(Event::Session);
            let _query = clone.guard(Event::Query("read"));
            let _attempt = clone.guard(Event::Attempt("replica"));
            let _connect = clone.guard(Event::Connect("node-a".into()));
            let _backend = clone.guard(Event::Backend("node-a".into()));
            ready.send(()).unwrap();
            std::future::pending::<()>().await;
        });
        rx.await.unwrap();
        assert!(exposition(&m).contains("rustybouncer_clients_active 1\n"));
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        let text = exposition(&m);
        for expected in [
            "rustybouncer_clients_active 0\n",
            "rustybouncer_queries_in_flight 0\n",
            "rustybouncer_sessions_completed_total{outcome=\"aborted\"} 1",
            "rustybouncer_queries_total{kind=\"read\",outcome=\"aborted\"} 1",
            "rustybouncer_query_attempts_total{outcome=\"aborted\",route=\"replica\"} 1",
            "rustybouncer_backend_connects_total{node=\"node-a\",outcome=\"aborted\"} 1",
            "rustybouncer_backend_connections_active{node=\"node-a\"} 0",
            "rustybouncer_session_duration_seconds_count 1",
            "rustybouncer_query_duration_seconds_count 1",
        ] {
            assert!(text.contains(expected), "{expected}\n{text}");
        }
    }
    #[test]
    fn pool_configuration_and_reset_completion_have_finite_explicit_accounting() {
        let m = enabled();
        m.configure_pool("session", 8, 2, 1);
        {
            let mut reset = m.guard(Event::PoolReset);
            reset.finish(Outcome::Ok);
        }
        {
            let _reset = m.guard(Event::PoolReset);
        }
        let text = exposition(&m);
        for expected in [
            "rustybouncer_pooling_enabled 1",
            "rustybouncer_pooling_mode{mode=\"session\"} 1",
            "rustybouncer_pooling_mode{mode=\"none\"} 0",
            "rustybouncer_pool_backend_capacity 8",
            "rustybouncer_pool_size 2",
            "rustybouncer_pool_resets_total{outcome=\"ok\"} 1",
            "rustybouncer_pool_resets_total{outcome=\"aborted\"} 1",
            "rustybouncer_pool_reset_duration_seconds_count 2",
        ] {
            assert!(text.contains(expected), "{expected}\n{text}");
        }
    }
    #[test]
    fn explicit_terminal_outcomes_are_once_only() {
        let m = enabled();
        {
            let mut g = m.guard(Event::Query("write"));
            g.finish(Outcome::SqlError);
            g.finish(Outcome::DeliveryError);
        }
        let text = exposition(&m);
        assert!(text
            .contains("rustybouncer_queries_total{kind=\"write\",outcome=\"delivery_error\"} 1"));
        assert!(text.contains("rustybouncer_queries_total{kind=\"write\",outcome=\"sql_error\"} 0"));
        assert!(text.contains("rustybouncer_query_duration_seconds_bucket{le=\"+Inf\"} 1"));
        assert!(text.contains("rustybouncer_query_duration_seconds_count 1"));
    }
    #[test]
    fn unchanged_rounds_refresh_eligibility_and_discovery() {
        let c = config();
        let m = enabled();
        let t = super::super::Topology {
            primary: Some(0),
            replicas: vec![1],
            ambiguous: false,
            generation: 1,
        };
        m.topology(&c.nodes, &t, true, true);
        m.set("topology_last_completed_timestamp_seconds", &[], 1);
        m.topology(&c.nodes, &t, false, false);
        let text = exposition(&m);
        assert!(!text.contains("rustybouncer_topology_last_completed_timestamp_seconds 1\n"));
        assert!(text.contains("rustybouncer_topology_changes_total 1"));
        assert!(text.contains("rustybouncer_primary_changes_total 1"));
        assert!(text.contains("rustybouncer_node_role{node=\"node-a\",role=\"primary\"} 1"));
        let ambiguous = super::super::Topology {
            primary: None,
            replicas: vec![2],
            ambiguous: true,
            generation: 2,
        };
        m.topology(&c.nodes, &ambiguous, true, true);
        assert!(exposition(&m)
            .contains("rustybouncer_node_role{node=\"node-a\",role=\"unavailable\"} 1"));
    }
    #[test]
    fn parses_request_without_using_untrusted_labels() {
        for (input, code) in [
            ("GET /metrics HTTP/1.1\r\nHost: local\r\n\r\n", 200),
            ("POST /metrics HTTP/1.1\r\n\r\n", 405),
            ("GET /no HTTP/1.0\r\n\r\n", 404),
        ] {
            assert_eq!(request(input.as_bytes()).unwrap(), code);
        }
        for input in [
            "broken\r\n\r\n",
            "GET /metrics HTTP/2\r\n\r\n",
            "GET /metrics HTTP/1.1\r\nbad header: x\r\n\r\n",
            "GET /metrics HTTP/1.1\r\nheader: \u{1}\r\n\r\n",
        ] {
            assert!(request(input.as_bytes()).is_err());
        }
    }
    async fn exchange(request: &[u8]) -> String {
        let (mut client, server) = super::super::tests::socket_pair().await;
        let task = tokio::spawn(http(server, enabled()));
        client.write_all(request).await.unwrap();
        let mut response = String::new();
        client.read_to_string(&mut response).await.unwrap();
        task.await.unwrap().unwrap();
        response
    }
    #[tokio::test]
    async fn endpoint_status_content_type_and_header_limit() {
        for (request, code) in [
            ("GET /metrics HTTP/1.1\r\n\r\n", 200),
            ("GET /other HTTP/1.1\r\n\r\n", 404),
            ("POST /metrics HTTP/1.1\r\n\r\n", 405),
            ("broken\r\n\r\n", 400),
        ] {
            let response = exchange(request.as_bytes()).await;
            assert!(response.starts_with(&format!("HTTP/1.1 {code}")));
            assert!(response.contains("Connection: close\r\n"));
            if code == 200 {
                assert!(response.contains("text/plain; version=0.0.4"));
                assert!(response.contains("# HELP rustybouncer_"));
            }
        }
        assert!(exchange(&vec![b'x'; 8192])
            .await
            .starts_with("HTTP/1.1 431"));
    }
    #[tokio::test]
    async fn scrape_capacity_is_bounded_without_an_accept_queue() {
        let m = enabled();
        let mut tasks = JoinSet::new();
        let mut clients = Vec::new();
        for _ in 0..16 {
            let (client, server) = super::super::tests::socket_pair().await;
            clients.push(client);
            assert!(spawn_scrape(&mut tasks, server, &m));
        }
        let (mut client, server) = super::super::tests::socket_pair().await;
        assert!(!spawn_scrape(&mut tasks, server, &m));
        assert_eq!(tasks.len(), 16);
        assert_eq!(client.read(&mut [0; 1]).await.unwrap(), 0);
        tasks.abort_all();
        while tasks.join_next().await.is_some() {}
    }
    #[tokio::test(start_paused = true)]
    async fn slow_headers_are_timed_out() {
        let (_client, server) = super::super::tests::socket_pair().await;
        let task = tokio::spawn(timeout(Duration::from_secs(2), http(server, enabled())));
        tokio::time::advance(Duration::from_secs(3)).await;
        assert!(task.await.unwrap().is_err());
    }
    #[tokio::test]
    async fn bind_conflict_and_unexpected_listener_exit_are_fatal() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        assert!(TcpListener::bind(listener.local_addr().unwrap())
            .await
            .is_err());
        assert!(listener_exit(tokio::spawn(async { Ok(()) }).await).is_err());
        let task = tokio::spawn(serve(listener, enabled()));
        task.abort();
        assert!(listener_exit(task.await).is_err());
    }
}
