//! Extended exchanges are buffered through their native terminal, never replayed.
//! Only the frontend's Sync closes an implicit transaction; private Flush does not.
use super::*;
use anyhow::Context as _;
use prepared::Message;
use wire::{Boundary, Reply};

struct Cycle {
    operation: cancel::Operation,
    accounting: Guard,
    node: Option<usize>,
    generation: u64,
    failed: bool,
    cancelled: bool,
}
#[derive(Default)]
pub(super) struct State {
    cache: Option<prepared::Cache>,
    cycle: Option<Cycle>,
    last_sql_error: bool,
}
pub(super) struct Context<'a> {
    pub client: &'a mut WriteHalf<transport::Stream>,
    pub config: &'a Config,
    pub topology: &'a mut watch::Receiver<Topology>,
    pub session: &'a mut SessionState,
    pub control: &'a Arc<cancel::Control>,
}
impl State {
    pub fn new(native: bool) -> Self {
        Self {
            cache: Some(prepared::Cache::new(128, 1024 * 1024, native)),
            cycle: None,
            last_sql_error: false,
        }
    }
    pub fn active(&self) -> bool {
        self.cycle.is_some()
    }
    pub fn failed(&self) -> bool {
        self.cycle.as_ref().is_some_and(|c| c.failed)
    }
    pub fn cache(&mut self) -> &mut prepared::Cache {
        self.cache.as_mut().unwrap()
    }
    pub async fn frame(
        &mut self,
        context: &mut Context<'_>,
        frame: &mut frontend::Incoming,
    ) -> Result<()> {
        self.last_sql_error = self.failed();
        if self.failed() && frame.tag != b'S' {
            return Ok(());
        }
        let message = Message::parse(frame.tag, &frame.body)?;
        if matches!(message, Message::Flush) {
            return Ok(());
        }
        self.begin(context)?;
        if !self.lease(context).await? {
            return Ok(());
        }
        if self.cycle.as_ref().unwrap().node.is_none() {
            return self.local_sync(context).await;
        }
        let reply = self.native(context, frame).await?;
        self.last_sql_error |= reply.response.sql_error;
        let operation = &self.cycle.as_ref().unwrap().operation;
        if let Err(error) =
            deliver(context.config, context.client, &reply.response, operation).await
        {
            if let Some(accounting) = &mut frame.accounting {
                accounting.finish(Outcome::DeliveryError);
            }
            self.cycle
                .as_mut()
                .unwrap()
                .accounting
                .finish(Outcome::DeliveryError);
            return Err(error.context(DeliveryFailed));
        }
        if reply.status.is_some() {
            self.complete(context).await?;
        }
        Ok(())
    }
    fn begin(&mut self, context: &Context<'_>) -> Result<()> {
        if self.cycle.is_none() {
            self.cycle = Some(Cycle {
                operation: context.control.begin()?,
                accounting: context.config.metrics.guard(Event::ExtendedCycle),
                node: None,
                generation: context.session.primary_generation,
                failed: false,
                cancelled: false,
            });
        }
        let cycle = self.cycle.as_ref().unwrap();
        if !cycle.failed && cycle.operation.interrupted().is_some() {
            bail!("cancellation between exchanges requires backend discard");
        }
        Ok(())
    }
    async fn lease(&mut self, context: &mut Context<'_>) -> Result<bool> {
        let cycle = self.cycle.as_mut().unwrap();
        if cycle.node.is_some() || cycle.failed {
            return Ok(true);
        }
        match acquire(context, &cycle.operation).await {
            Ok((node, generation)) => {
                cycle.node = Some(node);
                cycle.generation = generation;
                context.session.primary_generation = generation;
                context.session.unsynchronized = true;
                context.session.pinned |= context.config.pool_mode == pool::Mode::Session;
                Ok(true)
            }
            Err(error)
                if error.is::<cancel::Interrupted>()
                    && matches!(cycle.operation.interrupted(), Some(cancel::Reason::User)) =>
            {
                self.last_sql_error = true;
                cycle.failed = true;
                cycle.cancelled = true;
                error_reply(context.client).await?;
                Ok(false)
            }
            Err(error) => Err(error),
        }
    }
    async fn native(
        &mut self,
        context: &mut Context<'_>,
        frame: &mut frontend::Incoming,
    ) -> Result<Reply> {
        let message = Message::parse(frame.tag, &frame.body)?;
        let cycle = self.cycle.as_mut().unwrap();
        let cache = self.cache.as_mut().unwrap();
        let backend = context
            .session
            .backends
            .get_mut(&cycle.node.unwrap())
            .context("missing extended lease")?;
        before_native(cache, backend, &message);
        if !cache.native() && native_affinity(cache, &message) {
            materialize(context.config, context.topology, backend, cycle, cache).await?;
            context.session.pinned = true;
        }
        let restored = restore_needed(
            context.config,
            context.topology,
            backend,
            cycle,
            cache,
            &message,
        )
        .await?;
        let reply = match restored {
            Some(reply) => reply,
            None => {
                exchange(
                    context.config,
                    context.topology,
                    backend,
                    cycle,
                    frame.tag,
                    &frame.body,
                    boundary(&message),
                )
                .await?
            }
        };
        cycle.failed |= reply.response.sql_error;
        if !reply.response.sql_error {
            update_cache(backend, cache, &message)?;
            if frame.tag == b'P' && !cache.native() {
                remember_parse(
                    context.config,
                    context.topology,
                    backend,
                    cycle,
                    cache,
                    &mut frame.body,
                )
                .await?;
            }
        }
        if let Some(status) = reply.status {
            backend.status = status;
            context.session.status = status;
            context.session.transaction = status != b'I';
            if status == b'I' {
                cache.idle();
            }
            finish_sync(context.config, context.topology, backend, cycle, status).await?;
        }
        Ok(reply)
    }
    async fn complete(&mut self, context: &mut Context<'_>) -> Result<()> {
        let mut cycle = self.cycle.take().context("missing extended cycle")?;
        cycle.accounting.finish(if cycle.failed {
            Outcome::SqlError
        } else {
            Outcome::Ok
        });
        cycle
            .operation
            .finish(cleanup_deadline(context.config))
            .await?;
        context.session.unsynchronized = false;
        release_idle_transaction(context.config, context.topology, context.session).await;
        Ok(())
    }
    async fn local_sync(&mut self, context: &mut Context<'_>) -> Result<()> {
        send(context.client, b'Z', &[context.session.status]).await?;
        self.complete(context).await
    }
    // Simple Query is itself a native synchronization boundary. Never inject an
    // earlier Sync: that would commit an implicit transaction before this SQL.
    pub async fn simple(
        &mut self,
        context: &mut Context<'_>,
        frame: &mut frontend::Incoming,
    ) -> Result<()> {
        let accounting = frame
            .accounting
            .as_mut()
            .context("missing query accounting")?;
        if self.failed() {
            accounting.finish(Outcome::RouterError);
            return Ok(());
        }
        let cycle = self.cycle.as_mut().context("missing extended cycle")?;
        let node = cycle.node.context("missing extended backend")?;
        let backend = context
            .session
            .backends
            .get_mut(&node)
            .context("missing extended lease")?;
        materialize(
            context.config,
            context.topology,
            backend,
            cycle,
            self.cache.as_mut().unwrap(),
        )
        .await?;
        context.session.pinned = true;
        let reply = exchange(
            context.config,
            context.topology,
            backend,
            cycle,
            b'Q',
            &frame.body,
            Boundary::Simple,
        )
        .await?;
        cycle.failed |= reply.response.sql_error;
        let status = reply.status.context("missing Simple Ready status")?;
        backend.status = status;
        context.session.status = status;
        context.session.transaction = status != b'I';
        finish_sync(context.config, context.topology, backend, cycle, status).await?;
        self.cache.as_mut().unwrap().unnamed_query();
        if let Err(error) = deliver(
            context.config,
            context.client,
            &reply.response,
            &cycle.operation,
        )
        .await
        {
            accounting.finish(Outcome::DeliveryError);
            cycle.accounting.finish(Outcome::DeliveryError);
            return Err(error.context(DeliveryFailed));
        }
        accounting.finish(if reply.response.sql_error {
            Outcome::SqlError
        } else {
            Outcome::Ok
        });
        self.complete(context).await
    }
}
fn cleanup_deadline(config: &Config) -> Instant {
    Instant::now() + Duration::from_millis(config.cancel_cleanup_timeout_ms)
}
async fn error_reply(client: &mut WriteHalf<transport::Stream>) -> Result<()> {
    send(
        client,
        b'E',
        b"SERROR\0C57014\0Mcanceling statement due to user request\0\0",
    )
    .await
}
fn boundary(message: &Message<'_>) -> Boundary {
    match message {
        Message::Parse { .. } => Boundary::Parse,
        Message::Bind { .. } => Boundary::Bind,
        Message::Describe { kind: b'S', .. } => Boundary::DescribeStatement,
        Message::Describe { .. } => Boundary::DescribePortal,
        Message::Execute { .. } => Boundary::Execute,
        Message::Close { .. } => Boundary::Close,
        Message::Sync => Boundary::Sync,
        Message::Flush => unreachable!(),
    }
}
async fn acquire(context: &mut Context<'_>, operation: &cancel::Operation) -> Result<(usize, u64)> {
    let deadline = Instant::now() + Duration::from_millis(context.config.wait_timeout_ms);
    loop {
        operation.check()?;
        if Instant::now() >= deadline {
            return Err(pool::Rejected("extended lease deadline exhausted").into());
        }
        let failed = HashSet::new();
        let (node, selected) = tokio::select! {
            routed=timeout(deadline.saturating_duration_since(Instant::now()),wait_route(context.config,context.topology,false,&failed))=>routed??,
            reason=operation.cancelled()=>return Err(cancel::Interrupted(reason).into()),
        };
        context.session.validate_backend(node, &selected)?;
        match ensure_backend(
            context.config,
            context.session,
            (node, &selected),
            (false, deadline),
            operation,
        )
        .await?
        {
            Setup::Ready if *context.topology.borrow() == selected => {
                return Ok((node, selected.generation))
            }
            _ => {
                context.session.discard_unassigned(context.config.pool_mode);
                tokio::task::yield_now().await;
            }
        }
    }
}
async fn exchange(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    backend: &mut Backend,
    cycle: &mut Cycle,
    tag: u8,
    body: &[u8],
    boundary: Boundary,
) -> Result<Reply> {
    if !cycle.cancelled {
        let node = &config.nodes[cycle.node.context("missing extended node")?];
        cycle.operation.publish(Arc::new(cancel::Target {
            host: Arc::from(node.host.as_str()),
            port: node.port,
            pid: backend.cancel_identity.0,
            secret: backend.cancel_identity.1,
            tls: config.backend_tls.clone(),
            tls_server_name: node.tls_server_name.clone(),
        }))?;
    } else if !matches!(boundary, Boundary::Sync) {
        bail!("cancellation between extended exchanges; backend discarded");
    }
    let mut accounting = config.metrics.guard(Event::ExtendedExchange);
    let execution = async {
        send(&mut backend.stream, tag, body).await?;
        if !matches!(boundary, Boundary::Sync | Boundary::Simple) {
            send(&mut backend.stream, b'H', b"").await?;
        }
        wire::receive(&mut backend.stream, &backend.buffers, boundary).await
    };
    tokio::pin!(execution);
    let reply = tokio::select! {
        result=timeout(Duration::from_millis(config.query_timeout_ms),&mut execution)=>result.context("extended exchange timed out").and_then(|r|r),
        _reason=cycle.operation.cancelled()=>timeout(Duration::from_millis(config.cancel_cleanup_timeout_ms),&mut execution).await.context("cancelled extended exchange did not drain").and_then(|r|r),
        changed=primary_changed(topology,cycle.generation)=>Err(changed.err().unwrap_or_else(||anyhow::anyhow!("extended primary changed; not replayed"))),
    };
    finish_exchange(&mut accounting, &reply);
    let reply = reply?;
    cycle.cancelled |= cycle.operation.retire(cleanup_deadline(config)).await?;
    Ok(reply)
}
async fn finish_sync(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    backend: &mut Backend,
    cycle: &mut Cycle,
    status: u8,
) -> Result<()> {
    cycle.cancelled |= cycle.operation.retire(cleanup_deadline(config)).await?;
    if cycle.cancelled {
        let reply = exchange(config, topology, backend, cycle, b'S', b"", Boundary::Sync).await?;
        if reply.response.sql_error || reply.status != Some(status) {
            bail!("extended cancellation fence changed transaction state");
        }
    }
    Ok(())
}
async fn restore(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    backend: &mut Backend,
    cycle: &mut Cycle,
    definition: &prepared::Definition,
) -> Result<Option<Reply>> {
    if backend.installed.get(definition.name()) == Some(&definition.id) {
        return Ok(None);
    }
    // The two retained pieces avoid copying a large SQL body on every handoff.
    cycle.operation.check()?;
    let node = &config.nodes[cycle.node.context("missing restore node")?];
    cycle.operation.publish(Arc::new(cancel::Target {
        host: Arc::from(node.host.as_str()),
        port: node.port,
        pid: backend.cancel_identity.0,
        secret: backend.cancel_identity.1,
        tls: config.backend_tls.clone(),
        tls_server_name: node.tls_server_name.clone(),
    }))?;
    let mut accounting = config.metrics.guard(Event::ExtendedExchange);
    let reply = {
        let restoring = async {
            wire::send_parse(
                &mut backend.stream,
                definition.prefix(),
                &definition.resolved,
            )
            .await?;
            send(&mut backend.stream, b'H', b"").await?;
            wire::receive(&mut backend.stream, &backend.buffers, Boundary::Parse).await
        };
        tokio::pin!(restoring);
        tokio::select! {
            reply=timeout(Duration::from_millis(config.query_timeout_ms),&mut restoring)=>reply.map_err(anyhow::Error::from).and_then(|r|r),
            _reason=cycle.operation.cancelled()=>timeout(Duration::from_millis(config.cancel_cleanup_timeout_ms),&mut restoring).await.map_err(anyhow::Error::from).and_then(|r|r),
            changed=primary_changed(topology,cycle.generation)=>Err(changed.err().unwrap_or_else(||anyhow::anyhow!("restore primary changed"))),
        }
    };
    finish_exchange(&mut accounting, &reply);
    let reply = reply?;
    cycle.cancelled |= cycle.operation.retire(cleanup_deadline(config)).await?;
    if reply.response.sql_error {
        return Ok(Some(reply));
    }
    let description = describe(config, topology, backend, cycle, definition.name()).await?;
    if description.response.sql_error {
        return Ok(Some(description));
    }
    if description.response.shape()?.as_slice() != definition.shape.as_ref() {
        return Err(wire::ChangedShape.into());
    }
    backend
        .installed
        .insert(definition.name().to_vec(), definition.id);
    Ok(None)
}
async fn restore_needed(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    backend: &mut Backend,
    cycle: &mut Cycle,
    cache: &mut prepared::Cache,
    message: &Message<'_>,
) -> Result<Option<Reply>> {
    if cache.native() {
        return Ok(None);
    }
    let name = match message {
        Message::Parse { name, .. }
        | Message::Bind {
            statement: name, ..
        }
        | Message::Describe { kind: b'S', name }
        | Message::Close { kind: b'S', name } => Some(*name),
        _ => None,
    };
    if let Some(name) = name {
        if let Some(definition) = cache.get(name) {
            if !matches!(message,Message::Parse {name,..} if name.is_empty()) {
                return restore(config, topology, backend, cycle, &definition).await;
            }
        }
    }
    Ok(None)
}
fn update_cache(
    backend: &mut Backend,
    cache: &mut prepared::Cache,
    message: &Message<'_>,
) -> Result<()> {
    match message {
        Message::Parse { name, .. } => {
            cache.parse_started(name);
            backend.installed.remove(*name);
        }
        Message::Bind { portal, statement } => {
            cache.bind_started(portal);
            cache.bound(portal, statement)?;
        }
        Message::Close { kind, name } => {
            cache.close(*kind, name);
            if *kind == b'S' {
                backend.installed.remove(*name);
            }
        }
        _ => {}
    }
    Ok(())
}
async fn materialize(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    backend: &mut Backend,
    cycle: &mut Cycle,
    cache: &mut prepared::Cache,
) -> Result<()> {
    if cache.native() {
        return Ok(());
    }
    for definition in cache.all() {
        if restore(config, topology, backend, cycle, &definition)
            .await?
            .is_some()
        {
            bail!("prepared namespace materialization failed");
        }
    }
    cache.pin_native();
    Ok(())
}

fn before_native(cache: &mut prepared::Cache, backend: &mut Backend, message: &Message<'_>) {
    match message {
        Message::Parse { name, .. } if name.is_empty() => {
            cache.parse_started(name);
            backend.installed.remove(*name);
        }
        Message::Bind { portal, .. } if portal.is_empty() => cache.bind_started(portal),
        _ => {}
    }
}

pub(super) async fn dispatch(
    client: &mut WriteHalf<transport::Stream>,
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    session: &mut SessionState,
    control: &Arc<cancel::Control>,
    frame: &mut frontend::Incoming,
) -> Result<()> {
    let mut protocol = std::mem::take(&mut session.protocol);
    let result = {
        let mut context = Context {
            client,
            config,
            topology,
            session,
            control,
        };
        if frame.tag == b'Q' {
            protocol.simple(&mut context, frame).await
        } else {
            protocol.frame(&mut context, frame).await
        }
    };
    if frame.tag != b'Q' {
        if let Some(accounting) = &mut frame.accounting {
            if result.is_err() {
                accounting.finish_if_unfinished(Outcome::RouterError);
            } else {
                accounting.finish(if protocol.last_sql_error {
                    Outcome::SqlError
                } else {
                    Outcome::Ok
                });
            }
        }
    }
    if result.is_err() {
        if let Some(cycle) = &mut protocol.cycle {
            cycle.accounting.finish_if_unfinished(Outcome::RouterError);
        }
    }
    session.protocol = protocol;
    if let Err(error) = result {
        if let Some(accounting) = &mut frame.accounting {
            accounting.finish_if_unfinished(Outcome::RouterError);
        }
        if error.is::<DeliveryFailed>() {
            return Err(error);
        }
        let code = if error.is::<prepared::Malformed>() {
            "08P01"
        } else if error.is::<wire::CopyUnsupported>() || error.is::<wire::ChangedShape>() {
            "0A000"
        } else if error.is::<prepared::Exhausted>() || error.is::<wire::Limit>() {
            "54000"
        } else if error.is::<buffers::Exhausted>() {
            "53200"
        } else if error.is::<pool::Rejected>() {
            "53300"
        } else {
            "08006"
        };
        let _ = timeout(
            Duration::from_secs(1),
            pg_error(
                client,
                code,
                "rustybouncer: extended operation failed; backend discarded, no replay",
            ),
        )
        .await;
        return Err(error);
    }
    Ok(())
}

pub(super) fn needs_native(state: &mut SessionState, body: &[u8], kind: Kind) -> bool {
    let cache = state.protocol.cache();
    !cache.native() && cache.has_named() && !simple_namespace_independent(body, kind)
}
pub(super) fn simple_read_eligible(
    state: &SessionState,
    kind: Kind,
    mode: pool::Mode,
    native: bool,
) -> bool {
    !native && state.read_eligible(kind, mode)
}
fn simple_namespace_independent(body: &[u8], kind: Kind) -> bool {
    use sqlparser::{
        ast::{Statement, TableFactor, Visit, Visitor},
        dialect::PostgreSqlDialect,
        parser::Parser,
    };
    use std::ops::ControlFlow;
    struct NoTables;
    impl Visitor for NoTables {
        type Break = ();
        fn pre_visit_table_factor(&mut self, _: &TableFactor) -> ControlFlow<()> {
            ControlFlow::Break(())
        }
    }
    let Some(sql) = body
        .strip_suffix(b"\0")
        .and_then(|body| std::str::from_utf8(body).ok())
    else {
        return false;
    };
    let Ok(statements) = Parser::parse_sql(&PostgreSqlDialect {}, sql) else {
        return false;
    };
    statements.iter().all(|statement| match statement {
        Statement::StartTransaction { .. }
        | Statement::Commit { .. }
        | Statement::Rollback { .. }
        | Statement::Savepoint { .. }
        | Statement::ReleaseSavepoint { .. } => true,
        Statement::Query(_) => {
            !affinity::session_required(sql, kind) && statement.visit(&mut NoTables).is_continue()
        }
        _ => false,
    })
}

pub(super) async fn simple_backend(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    state: &mut SessionState,
    node: usize,
    operation: &cancel::Operation,
    native: bool,
) -> Result<()> {
    let backend = state
        .backends
        .get_mut(&node)
        .context("missing simple backend")?;
    let cache = state.protocol.cache();
    if native || (state.pinned && !cache.native()) {
        state.pinned = true;
        state.primary_generation = backend.generation;
        for definition in cache.all() {
            restore_simple(config, topology, backend, operation, node, &definition).await?;
        }
        cache.pin_native();
    }
    cache.unnamed_query();
    backend.installed.remove(&b""[..]);
    Ok(())
}
async fn restore_simple(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    backend: &mut Backend,
    operation: &cancel::Operation,
    node: usize,
    definition: &prepared::Definition,
) -> Result<()> {
    if backend.installed.get(definition.name()) == Some(&definition.id) {
        return Ok(());
    }
    let target = &config.nodes[node];
    operation.publish(Arc::new(cancel::Target {
        host: Arc::from(target.host.as_str()),
        port: target.port,
        pid: backend.cancel_identity.0,
        secret: backend.cancel_identity.1,
        tls: config.backend_tls.clone(),
        tls_server_name: target.tls_server_name.clone(),
    }))?;
    let mut accounting = config.metrics.guard(Event::ExtendedExchange);
    let reply = {
        let restoring = async {
            wire::send_parse(
                &mut backend.stream,
                definition.prefix(),
                &definition.resolved,
            )
            .await?;
            send(&mut backend.stream, b'H', b"").await?;
            wire::receive(&mut backend.stream, &backend.buffers, Boundary::Parse).await
        };
        tokio::pin!(restoring);
        tokio::select! {
            reply=timeout(Duration::from_millis(config.query_timeout_ms),&mut restoring)=>reply.map_err(anyhow::Error::from).and_then(|r|r),
            _reason=operation.cancelled()=>timeout(Duration::from_millis(config.cancel_cleanup_timeout_ms),&mut restoring).await.map_err(anyhow::Error::from).and_then(|r|r),
            changed=primary_changed(topology,backend.generation)=>Err(changed.err().unwrap_or_else(||anyhow::anyhow!("namespace restore primary changed"))),
        }
    };
    finish_exchange(&mut accounting, &reply);
    let reply = reply?;
    if operation.retire(cleanup_deadline(config)).await? || reply.response.sql_error {
        bail!("prepared namespace restore interrupted or rejected");
    }
    let mut description = Vec::with_capacity(definition.name().len() + 2);
    description.push(b'S');
    description.extend_from_slice(definition.name());
    description.push(0);
    let reply = describe_simple(config, topology, backend, operation, node, &description).await?;
    if reply.response.sql_error || reply.response.shape()?.as_slice() != definition.shape.as_ref() {
        return Err(wire::ChangedShape.into());
    }
    backend
        .installed
        .insert(definition.name().to_vec(), definition.id);
    Ok(())
}

pub(super) fn frame_label(tag: u8) -> Option<&'static str> {
    Some(match tag {
        b'P' => "parse",
        b'B' => "bind",
        b'D' => "describe",
        b'E' => "execute",
        b'C' => "close",
        b'H' => "flush",
        b'S' => "sync",
        b'X' => return None,
        _ => "malformed",
    })
}

async fn remember_parse(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    backend: &mut Backend,
    cycle: &mut Cycle,
    cache: &mut prepared::Cache,
    body: &mut Vec<u8>,
) -> Result<()> {
    let Message::Parse { name, .. } = Message::parse(b'P', body)? else {
        unreachable!()
    };
    let mut describe = Vec::with_capacity(name.len() + 2);
    describe.push(b'S');
    describe.extend_from_slice(name);
    describe.push(0);
    let reply = exchange(
        config,
        topology,
        backend,
        cycle,
        b'D',
        &describe,
        Boundary::DescribeStatement,
    )
    .await?;
    if reply.response.sql_error {
        bail!("could not resolve prepared parameter types");
    }
    let definition = cache.insert(
        std::mem::take(body),
        reply.response.parameter_types()?,
        reply.response.shape()?,
    )?;
    backend
        .installed
        .insert(definition.name().to_vec(), definition.id);
    Ok(())
}

fn finish_exchange(accounting: &mut Guard, result: &Result<Reply>) {
    accounting.finish(match result {
        Ok(reply) if reply.response.sql_error => Outcome::SqlError,
        Ok(_) => Outcome::Ok,
        Err(_) => Outcome::Error,
    });
}

async fn deliver(
    config: &Config,
    client: &mut WriteHalf<transport::Stream>,
    response: &Response,
    operation: &cancel::Operation,
) -> Result<()> {
    let sequence = operation.sequence();
    tokio::select! {
        biased;
        result=timeout(Duration::from_millis(config.delivery_timeout_ms),response.write_to(client))=>result.context("extended frontend delivery timed out").and_then(|r|r),
        reason=operation.delivery_interrupted(sequence)=>Err(cancel::Interrupted(reason).into()),
    }
}

#[derive(Debug)]
struct DeliveryFailed;
impl std::fmt::Display for DeliveryFailed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("extended response delivery interrupted")
    }
}
impl std::error::Error for DeliveryFailed {}

fn native_affinity(cache: &prepared::Cache, message: &Message<'_>) -> bool {
    match message {
        // Unknown session effects retain ownership before planning as well as
        // execution: an immutable function can be evaluated during Bind.
        Message::Parse { sql, .. } => prepared::session_required(sql),
        Message::Execute { portal } => cache.executed_session_effect(portal),
        _ => false,
    }
}

async fn describe(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    backend: &mut Backend,
    cycle: &mut Cycle,
    name: &[u8],
) -> Result<Reply> {
    let mut body = Vec::with_capacity(name.len() + 2);
    body.push(b'S');
    body.extend_from_slice(name);
    body.push(0);
    exchange(
        config,
        topology,
        backend,
        cycle,
        b'D',
        &body,
        Boundary::DescribeStatement,
    )
    .await
}
async fn describe_simple(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    backend: &mut Backend,
    operation: &cancel::Operation,
    node: usize,
    body: &[u8],
) -> Result<Reply> {
    let target = &config.nodes[node];
    operation.publish(Arc::new(cancel::Target {
        host: Arc::from(target.host.as_str()),
        port: target.port,
        pid: backend.cancel_identity.0,
        secret: backend.cancel_identity.1,
        tls: config.backend_tls.clone(),
        tls_server_name: target.tls_server_name.clone(),
    }))?;
    let mut accounting = config.metrics.guard(Event::ExtendedExchange);
    let reading = async {
        send(&mut backend.stream, b'D', body).await?;
        send(&mut backend.stream, b'H', b"").await?;
        wire::receive(
            &mut backend.stream,
            &backend.buffers,
            Boundary::DescribeStatement,
        )
        .await
    };
    tokio::pin!(reading);
    let result = tokio::select! {
        result=timeout(Duration::from_millis(config.query_timeout_ms),&mut reading)=>result.map_err(anyhow::Error::from).and_then(|r|r),
        _reason=operation.cancelled()=>timeout(Duration::from_millis(config.cancel_cleanup_timeout_ms),&mut reading).await.map_err(anyhow::Error::from).and_then(|r|r),
        changed=primary_changed(topology,backend.generation)=>Err(changed.err().unwrap_or_else(||anyhow::anyhow!("namespace describe primary changed"))),
    };
    finish_exchange(&mut accounting, &result);
    let reply = result?;
    if operation.retire(cleanup_deadline(config)).await? {
        bail!("namespace description cancelled");
    }
    Ok(reply)
}
