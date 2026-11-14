//! A bounded reader remains live during SQL execution and response delivery.
//! Both futures belong to the session task; abort drops both, never detaches work.
use super::*;
use tokio::{
    io::ReadHalf,
    sync::{mpsc, OwnedSemaphorePermit},
};

pub(super) struct Incoming {
    pub tag: u8,
    pub body: Vec<u8>,
    pub kind: Option<Kind>,
    pub accounting: Option<Guard>,
    _bytes: OwnedSemaphorePermit,
}

impl Incoming {
    pub fn reject(&mut self) {
        if let Some(guard) = &mut self.accounting {
            guard.finish(Outcome::RouterError);
        }
    }
}

pub(super) async fn read(
    mut stream: ReadHalf<transport::Stream>,
    sender: mpsc::Sender<Incoming>,
    control: Arc<cancel::Control>,
    config: Arc<Config>,
) -> Result<bool> {
    let bytes = Arc::new(Semaphore::new(MAX_MESSAGE));
    let reading = async {
        loop {
            let tag = match stream.read_u8().await {
                Ok(tag) => tag,
                Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => {
                    return Ok(false)
                }
                Err(error) => return Err(error.into()),
            };
            let (_, body) = message_body(&mut stream, tag).await?;
            let mut accounting = if tag == b'Q' {
                Some(config.metrics.guard(Event::Query("malformed")))
            } else {
                protocol::frame_label(tag)
                    .map(|kind| config.metrics.guard(Event::ExtendedFrame(kind)))
            };
            let kind = (tag == b'Q').then(|| query_kind(&body)).flatten();
            if let Some(guard) = &mut accounting {
                guard.classify_query(kind.map(Kind::label).unwrap_or("malformed"));
            }
            let permit = bytes
                .clone()
                .try_acquire_many_owned(body.len().max(1) as u32)
                .map_err(|error| {
                    if let Some(guard) = &mut accounting {
                        guard.finish(Outcome::RouterError);
                    }
                    anyhow::Error::from(error).context("frontend pipeline byte capacity exceeded")
                })?;
            sender
                .try_send(Incoming {
                    tag,
                    body,
                    kind,
                    accounting,
                    _bytes: permit,
                })
                .map_err(|error| {
                    let mut frame = error.into_inner();
                    frame.reject();
                    anyhow::anyhow!("frontend pipeline frame capacity exceeded")
                })?;
            if tag == b'X' {
                return Ok::<_, anyhow::Error>(true);
            }
        }
    }
    .await;
    drop(sender);
    if !matches!(reading, Ok(true)) {
        if let Some(delivery) = control.disconnect() {
            delivery
                .send(Instant::now() + Duration::from_millis(config.cancel_cleanup_timeout_ms))
                .await?;
        }
    }
    reading
}

#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Arc<Config> {
        let mut c: Config = toml::from_str(include_str!("../../rustybouncer.toml")).unwrap();
        c.metrics = Metrics::new(true, &c.nodes, true, c.max_clients).unwrap();
        c.cancellations = cancel::Registry::new(1);
        Arc::new(c)
    }
    #[tokio::test]
    async fn clean_eof_and_truncated_or_oversized_frames_are_distinct() {
        for (bytes, clean) in [
            (Vec::new(), true),
            (vec![b'Q', 0, 0], false),
            (vec![b'Q', 127, 255, 255, 255], false),
        ] {
            let c = config();
            let registration = c.cancellations.register().unwrap();
            let (mut client, server) = crate::tests::socket_pair().await;
            let (reader, _writer) = tokio::io::split(transport::Stream::from(server));
            let (sender, _receiver) = mpsc::channel(256);
            client.write_all(&bytes).await.unwrap();
            client.shutdown().await.unwrap();
            let result = read(reader, sender, registration.control.clone(), c).await;
            assert_eq!(result.is_ok(), clean);
            if clean {
                assert!(!result.unwrap());
            }
        }
    }
    #[tokio::test]
    async fn overflow_counts_all_complete_queries_and_returns_all_queue_capacity() {
        let c = config();
        let registration = c.cancellations.register().unwrap();
        let (mut client, server) = crate::tests::socket_pair().await;
        let (reader, _writer) = tokio::io::split(transport::Stream::from(server));
        let (sender, mut receiver) = mpsc::channel(256);
        for _ in 0..257 {
            send(&mut client, b'Q', b"SELECT 1\0").await.unwrap();
        }
        let result = read(reader, sender, registration.control.clone(), c.clone()).await;
        assert!(result.is_err());
        let mut count = 0;
        while let Some(mut frame) = receiver.recv().await {
            frame.reject();
            count += 1;
        }
        assert_eq!(count, 256);
        let text = String::from_utf8(c.metrics.render().unwrap()).unwrap();
        assert!(
            text.contains("rustybouncer_queries_total{kind=\"read\",outcome=\"router_error\"} 257")
        );
        assert!(text.contains("rustybouncer_queries_in_flight 0"));
        assert!(text.contains("rustybouncer_queries_total{kind=\"read\",outcome=\"aborted\"} 0"));
    }
}
