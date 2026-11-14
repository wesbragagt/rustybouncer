//! Virtual frontend identities and operation-scoped PostgreSQL cancellation.
use anyhow::{anyhow, bail, Context, Result};
use std::{
    collections::HashMap,
    fmt,
    sync::{
        atomic::{AtomicU32, AtomicUsize, Ordering},
        Arc, Mutex,
    },
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    sync::{watch, Notify},
    time::{timeout_at, Instant},
};

pub struct Target {
    pub host: Arc<str>,
    pub port: u16,
    pub pid: u32,
    pub secret: u32,
    pub tls: Option<crate::transport::Tls>,
    pub tls_server_name: Option<String>,
}
impl Target {
    async fn deliver(&self, deadline: Instant) -> Result<()> {
        timeout_at(deadline, async {
            let socket = TcpStream::connect((self.host.as_ref(), self.port)).await?;
            let mut socket = crate::transport::backend(
                socket,
                self.tls_server_name.as_deref().unwrap_or(&self.host),
                self.tls.as_ref(),
            )
            .await?;
            let mut packet = [0u8; 16];
            for (index, value) in [16, 80877102, self.pid, self.secret]
                .into_iter()
                .enumerate()
            {
                packet[index * 4..index * 4 + 4].copy_from_slice(&value.to_be_bytes());
            }
            socket.write_all(&packet).await?;
            socket.shutdown().await?;
            let mut reply = [0u8; 1];
            if socket.read(&mut reply).await? != 0 {
                bail!("unexpected cancellation reply");
            }
            Ok::<_, anyhow::Error>(())
        })
        .await
        .context("cancellation delivery timed out")?
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reason {
    User,
    Disconnected,
}
#[derive(Debug)]
pub struct Interrupted(pub Reason);
impl fmt::Display for Interrupted {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self.0 {
            Reason::User => "query cancelled",
            Reason::Disconnected => "frontend disconnected",
        })
    }
}
impl std::error::Error for Interrupted {}

#[derive(Clone, Copy, Default)]
struct Signal {
    epoch: u64,
    cancelled: bool,
    disconnected: bool,
    requests: u64,
}
#[derive(Default)]
struct State {
    epoch: u64,
    running: bool,
    finishing: bool,
    cancelled: bool,
    disconnected: bool,
    requests: u64,
    delivery_failed: bool,
    target: Option<Arc<Target>>,
}
pub struct Control {
    state: Mutex<State>,
    changed: watch::Sender<Signal>,
    pending: AtomicUsize,
    delivered: Notify,
}
impl Control {
    fn new() -> Self {
        let (changed, _) = watch::channel(Signal::default());
        Self {
            state: Mutex::new(State::default()),
            changed,
            pending: AtomicUsize::new(0),
            delivered: Notify::new(),
        }
    }
    fn announce(&self, state: &State) {
        self.changed.send_replace(Signal {
            epoch: state.epoch,
            cancelled: state.cancelled,
            disconnected: state.disconnected,
            requests: state.requests,
        });
    }
    pub fn begin(self: &Arc<Self>) -> Result<Operation> {
        let mut state = self.state.lock().unwrap();
        if state.disconnected {
            return Err(Interrupted(Reason::Disconnected).into());
        }
        if state.running || state.finishing || self.pending.load(Ordering::Acquire) != 0 {
            bail!("previous operation cleanup incomplete");
        }
        state.epoch = state
            .epoch
            .checked_add(1)
            .context("operation identity exhausted")?;
        state.running = true;
        state.cancelled = false;
        state.delivery_failed = false;
        state.target = None;
        self.announce(&state);
        Ok(Operation {
            control: self.clone(),
            epoch: state.epoch,
            finished: false,
        })
    }
    fn interrupt(self: &Arc<Self>, reason: Reason) -> Option<Delivery> {
        let mut state = self.state.lock().unwrap();
        if state.disconnected {
            return None;
        }
        state.disconnected = reason == Reason::Disconnected;
        if !state.running {
            self.announce(&state);
            return None;
        }
        state.cancelled = true;
        state.requests = state.requests.checked_add(1).unwrap_or_else(|| {
            state.disconnected = true;
            u64::MAX
        });
        let ticket = state.target.as_ref().map(|_| {
            self.pending.fetch_add(1, Ordering::AcqRel);
            Pending {
                control: self.clone(),
                epoch: state.epoch,
                clean: false,
            }
        });
        let delivery = Delivery {
            target: state.target.clone(),
            ticket,
        };
        self.announce(&state);
        Some(delivery)
    }
    pub fn disconnect(self: &Arc<Self>) -> Option<Delivery> {
        self.interrupt(Reason::Disconnected)
    }
    async fn wait_delivered(&self, deadline: Instant) -> Result<()> {
        timeout_at(deadline, async {
            loop {
                let notified = self.delivered.notified();
                if self.pending.load(Ordering::Acquire) == 0 {
                    break;
                }
                notified.await;
            }
        })
        .await
        .context("cancellation cleanup timed out")
    }
}

struct Pending {
    control: Arc<Control>,
    epoch: u64,
    clean: bool,
}
impl Drop for Pending {
    fn drop(&mut self) {
        if !self.clean {
            let mut state = self.control.state.lock().unwrap();
            if state.epoch == self.epoch {
                state.delivery_failed = true;
            }
        }
        let previous = self.control.pending.fetch_sub(1, Ordering::AcqRel);
        debug_assert!(previous > 0);
        self.control.delivered.notify_waiters();
    }
}
pub struct Delivery {
    target: Option<Arc<Target>>,
    ticket: Option<Pending>,
}
impl Delivery {
    pub async fn send(mut self, deadline: Instant) -> Result<()> {
        let response = match &self.target {
            Some(target) => target.deliver(deadline).await,
            None => Ok(()),
        };
        if let Some(ticket) = &mut self.ticket {
            ticket.clean = response.is_ok();
        }
        drop(self.ticket);
        response
    }
}

pub struct Operation {
    control: Arc<Control>,
    epoch: u64,
    finished: bool,
}
impl Operation {
    pub fn interrupted(&self) -> Option<Reason> {
        let state = self.control.state.lock().unwrap();
        if state.epoch != self.epoch || state.disconnected {
            Some(Reason::Disconnected)
        } else if state.cancelled {
            Some(Reason::User)
        } else {
            None
        }
    }
    pub fn check(&self) -> Result<()> {
        match self.interrupted() {
            Some(reason) => Err(Interrupted(reason).into()),
            None => Ok(()),
        }
    }
    pub fn publish(&self, target: Arc<Target>) -> Result<()> {
        let mut state = self.control.state.lock().unwrap();
        if state.epoch != self.epoch || !state.running || state.disconnected {
            return Err(Interrupted(Reason::Disconnected).into());
        }
        if state.cancelled {
            return Err(Interrupted(Reason::User).into());
        }
        state.target = Some(target);
        Ok(())
    }
    pub fn clear_target(&self) {
        let mut state = self.control.state.lock().unwrap();
        if state.epoch == self.epoch {
            state.target = None;
        }
    }
    // Retire the physical target while retaining the logical operation through
    // frontend delivery. Accepted cancel senders finish before a barrier/reuse.
    pub async fn retire(&self, deadline: Instant) -> Result<bool> {
        self.clear_target();
        self.control.wait_delivered(deadline).await?;
        let state = self.control.state.lock().unwrap();
        if state.delivery_failed {
            bail!("cancellation delivery failed; discard backend");
        }
        Ok(state.cancelled)
    }
    pub fn sequence(&self) -> u64 {
        self.control.state.lock().unwrap().requests
    }
    pub async fn delivery_interrupted(&self, sequence: u64) -> Reason {
        let mut changed = self.control.changed.subscribe();
        loop {
            let signal = *changed.borrow_and_update();
            if signal.disconnected || signal.epoch != self.epoch {
                return Reason::Disconnected;
            }
            if signal.requests > sequence {
                return Reason::User;
            }
            if changed.changed().await.is_err() {
                return Reason::Disconnected;
            }
        }
    }
    pub async fn cancelled(&self) -> Reason {
        let mut changed = self.control.changed.subscribe();
        loop {
            let current = *changed.borrow_and_update();
            if current.disconnected || current.epoch != self.epoch {
                return Reason::Disconnected;
            }
            if current.cancelled {
                return Reason::User;
            }
            if changed.changed().await.is_err() {
                return Reason::Disconnected;
            }
        }
    }
    pub async fn finish(mut self, deadline: Instant) -> Result<()> {
        {
            let mut state = self.control.state.lock().unwrap();
            if state.epoch != self.epoch || !state.running {
                bail!("stale operation completion");
            }
            state.running = false;
            state.finishing = true;
            state.target = None;
        }
        if let Err(error) = self.control.wait_delivered(deadline).await {
            let mut state = self.control.state.lock().unwrap();
            state.disconnected = true;
            state.finishing = false;
            self.control.announce(&state);
            drop(state);
            self.finished = true;
            return Err(error);
        }
        let completion = {
            let mut state = self.control.state.lock().unwrap();
            if state.delivery_failed {
                state.disconnected = true;
                self.control.announce(&state);
            }
            state.finishing = false;
            if state.delivery_failed {
                None
            } else {
                Some(())
            }
        };
        self.finished = true;
        completion.context("cancellation delivery failed; backend must be discarded")
    }
}
impl Drop for Operation {
    fn drop(&mut self) {
        if !self.finished {
            let mut state = self.control.state.lock().unwrap();
            if state.epoch == self.epoch {
                state.running = false;
                state.finishing = false;
                state.target = None;
                state.disconnected = true;
                self.control.announce(&state);
            }
        }
    }
}

struct RegistryInner {
    entries: Mutex<HashMap<u32, (u32, Arc<Control>)>>,
    next: AtomicU32,
    capacity: usize,
}
#[derive(Clone)]
pub struct Registry(Arc<RegistryInner>);
impl Default for Registry {
    fn default() -> Self {
        Self::new(0)
    }
}
impl Registry {
    pub fn new(capacity: usize) -> Self {
        Self(Arc::new(RegistryInner {
            entries: Mutex::new(HashMap::new()),
            next: AtomicU32::new(1),
            capacity,
        }))
    }
    pub fn register(&self) -> Result<Registration> {
        let mut entries = self.0.entries.lock().unwrap();
        if entries.len() >= self.0.capacity {
            bail!("cancellation registry capacity exhausted");
        }
        entries.try_reserve(1)?;
        let mut secret = [0u8; 4];
        getrandom::fill(&mut secret)
            .map_err(|_| anyhow!("secure cancellation identity unavailable"))?;
        for _ in 0..entries.len() + 2 {
            let pid = self.0.next.fetch_add(1, Ordering::Relaxed) & 0x7fffffff;
            if pid == 0 || entries.contains_key(&pid) {
                continue;
            }
            let control = Arc::new(Control::new());
            let secret = u32::from_ne_bytes(secret);
            entries.insert(pid, (secret, control.clone()));
            return Ok(Registration {
                registry: self.clone(),
                pid,
                secret,
                control,
            });
        }
        bail!("cancellation identity space exhausted")
    }
    pub async fn request(&self, pid: u32, secret: u32, deadline: Instant) -> Result<bool> {
        let control = {
            let entries = self.0.entries.lock().unwrap();
            entries
                .get(&pid)
                .filter(|entry| entry.0 == secret)
                .map(|entry| entry.1.clone())
        };
        let Some(control) = control else {
            return Ok(false);
        };
        let Some(delivery) = control.interrupt(Reason::User) else {
            return Ok(false);
        };
        delivery.send(deadline).await?;
        Ok(true)
    }
}
pub struct Registration {
    registry: Registry,
    pid: u32,
    secret: u32,
    pub control: Arc<Control>,
}
impl Registration {
    pub fn identity(&self) -> (u32, u32) {
        (self.pid, self.secret)
    }
}
impl Drop for Registration {
    fn drop(&mut self) {
        let mut entries = self.registry.0.entries.lock().unwrap();
        if entries
            .get(&self.pid)
            .is_some_and(|entry| Arc::ptr_eq(&entry.1, &self.control))
        {
            entries.remove(&self.pid);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::{net::TcpListener, sync::oneshot, time::Duration};
    fn deadline() -> Instant {
        Instant::now() + Duration::from_secs(2)
    }
    #[tokio::test]
    async fn registration_is_bounded_and_wrong_or_stale_secrets_do_nothing() {
        let registry = Registry::new(1);
        let registration = registry.register().unwrap();
        assert!(registry.register().is_err());
        let (pid, secret) = registration.identity();
        let operation = registration.control.begin().unwrap();
        assert!(!registry.request(pid, secret ^ 1, deadline()).await.unwrap());
        operation.check().unwrap();
        operation.finish(deadline()).await.unwrap();
        drop(registration);
        assert!(!registry.request(pid, secret, deadline()).await.unwrap());
        assert!(registry.register().is_ok());
    }
    #[tokio::test]
    async fn idle_cancel_is_not_saved_for_the_next_operation_and_cancel_stops_retries() {
        let registry = Registry::new(1);
        let registration = registry.register().unwrap();
        let (pid, secret) = registration.identity();
        assert!(!registry.request(pid, secret, deadline()).await.unwrap());
        let operation = registration.control.begin().unwrap();
        operation.check().unwrap();
        assert!(registry.request(pid, secret, deadline()).await.unwrap());
        assert_eq!(operation.cancelled().await, Reason::User);
        assert!(operation.check().unwrap_err().is::<Interrupted>());
        assert!(operation.retire(deadline()).await.unwrap());
        operation.finish(deadline()).await.unwrap();
        let next = registration.control.begin().unwrap();
        next.check().unwrap();
        next.finish(deadline()).await.unwrap();
    }
    #[tokio::test]
    async fn accepted_native_delivery_is_fenced_before_completion_and_next_operation() {
        let registry = Registry::new(1);
        let registration = registry.register().unwrap();
        let (pid, secret) = registration.identity();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let operation = registration.control.begin().unwrap();
        operation
            .publish(Arc::new(Target {
                host: Arc::from("127.0.0.1"),
                port: listener.local_addr().unwrap().port(),
                pid: 123,
                secret: 456,
                tls: None,
                tls_server_name: None,
            }))
            .unwrap();
        let request = tokio::spawn(async move { registry.request(pid, secret, deadline()).await });
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut bytes = [0u8; 16];
        socket.read_exact(&mut bytes).await.unwrap();
        assert_eq!(
            bytes,
            [16u32, 80877102, 123, 456]
                .into_iter()
                .flat_map(u32::to_be_bytes)
                .collect::<Vec<_>>()
                .as_slice()
        );
        assert_eq!(registration.control.pending.load(Ordering::Acquire), 1);
        assert!(registration.control.begin().is_err());
        let (started, observed) = oneshot::channel();
        let completing = tokio::spawn(async move {
            let _ = started.send(());
            operation.finish(deadline()).await
        });
        observed.await.unwrap();
        // Native EOF has deliberately not been released at this barrier.
        assert!(!completing.is_finished());
        socket.shutdown().await.unwrap();
        assert!(request.await.unwrap().unwrap());
        completing.await.unwrap().unwrap();
        let next = registration.control.begin().unwrap();
        next.check().unwrap();
        next.finish(deadline()).await.unwrap();
    }
    #[tokio::test]
    async fn dropped_delivery_and_aborted_operation_cannot_publish_reusable_capacity() {
        let control = Arc::new(Control::new());
        let operation = control.begin().unwrap();
        operation
            .publish(Arc::new(Target {
                host: Arc::from("127.0.0.1"),
                port: 1,
                pid: 1,
                secret: 2,
                tls: None,
                tls_server_name: None,
            }))
            .unwrap();
        let delivery = control.interrupt(Reason::User).unwrap();
        drop(delivery);
        assert!(operation.retire(deadline()).await.is_err());
        assert!(operation.finish(deadline()).await.is_err());
        assert!(control.begin().is_err());
        let other = Arc::new(Control::new());
        drop(other.begin().unwrap());
        assert!(other.begin().is_err());
    }
    #[tokio::test]
    async fn delivery_watches_new_requests_and_disconnect_after_native_cancel() {
        let control = Arc::new(Control::new());
        let operation = control.begin().unwrap();
        control
            .interrupt(Reason::User)
            .unwrap()
            .send(deadline())
            .await
            .unwrap();
        let sequence = operation.sequence();
        // A subsequent request during frontend delivery has no native target.
        control
            .interrupt(Reason::User)
            .unwrap()
            .send(deadline())
            .await
            .unwrap();
        assert_eq!(operation.delivery_interrupted(sequence).await, Reason::User);
        control
            .disconnect()
            .unwrap()
            .send(deadline())
            .await
            .unwrap();
        assert_eq!(
            operation.delivery_interrupted(operation.sequence()).await,
            Reason::Disconnected
        );
        operation.finish(deadline()).await.unwrap();
    }
}
