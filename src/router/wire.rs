//! Private complete responses: a failed attempt never donates bytes to a retry.
use super::*;
use std::io::{Error, ErrorKind, IoSlice};
use tokio::io::{AsyncRead, AsyncWrite};

const MAX_RESULT: usize = 32 * 1024 * 1024;

pub(super) async fn send<W: AsyncWrite + Unpin>(
    writer: &mut W,
    tag: u8,
    body: &[u8],
) -> Result<()> {
    let length = u32::try_from(body.len().checked_add(4).context("frame length overflow")?)?;
    let mut header = [0; 5];
    header[0] = tag;
    header[1..].copy_from_slice(&length.to_be_bytes());
    let mut parts = [IoSlice::new(&header), IoSlice::new(body)];
    let mut remaining = &mut parts[..];
    while !remaining.is_empty() {
        let count = writer.write_vectored(remaining).await?;
        if count == 0 {
            return Err(Error::new(ErrorKind::WriteZero, "protocol write made no progress").into());
        }
        IoSlice::advance_slices(&mut remaining, count);
    }
    Ok(())
}

pub(super) struct Response {
    small: [u8; buffers::SMALL_BYTES],
    small_len: usize,
    bulk: Vec<buffers::Slab>,
    len: usize,
    pub(super) sql_error: bool,
}
impl std::fmt::Debug for Response {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Response")
            .field("length", &self.len)
            .finish()
    }
}
impl Response {
    fn reserve_frame(&self, length: usize) -> Result<usize> {
        let end = self
            .len
            .checked_add(length)
            .context("result length overflow")?;
        if end > MAX_RESULT {
            bail!("routed result exceeds 32 MiB limit");
        }
        Ok(end)
    }
    fn writable(&mut self, wanted: usize, pool: &buffers::Pool) -> Result<&mut [u8]> {
        if self.bulk.is_empty() && self.small_len < buffers::SMALL_BYTES {
            let count = wanted.min(buffers::SMALL_BYTES - self.small_len);
            let start = self.small_len;
            self.small_len += count;
            self.len += count;
            return Ok(&mut self.small[start..self.small_len]);
        }
        if self.bulk.last().is_none_or(|slab| slab.remaining() == 0) {
            if self.bulk.len() == self.bulk.capacity() {
                let capacity = (self.bulk.len() + 1)
                    .next_power_of_two()
                    .min(MAX_RESULT / buffers::SLAB_BYTES);
                self.bulk.try_reserve_exact(capacity - self.bulk.len())?;
            }
            let mut slab = pool.try_get()?;
            if self.bulk.is_empty() {
                slab.reserve(self.small_len)
                    .copy_from_slice(&self.small[..self.small_len]);
                self.small_len = 0;
            }
            self.bulk.push(slab);
        }
        let slab = self.bulk.last_mut().unwrap();
        let count = wanted.min(slab.remaining());
        self.len += count;
        Ok(slab.reserve(count))
    }
    fn append(&mut self, mut bytes: &[u8], pool: &buffers::Pool) -> Result<()> {
        while !bytes.is_empty() {
            let target = self.writable(bytes.len(), pool)?;
            let count = target.len();
            target.copy_from_slice(&bytes[..count]);
            bytes = &bytes[count..];
        }
        Ok(())
    }
    pub(super) async fn write_to<W: AsyncWrite + Unpin>(&self, writer: &mut W) -> Result<()> {
        if self.bulk.is_empty() {
            writer.write_all(&self.small[..self.small_len]).await?;
        } else {
            for slab in &self.bulk {
                writer.write_all(slab.data()).await?;
            }
        }
        Ok(())
    }
    fn last_byte(&self) -> u8 {
        self.bulk
            .last()
            .map(|slab| *slab.data().last().unwrap())
            .unwrap_or_else(|| self.small[self.small_len - 1])
    }
    #[cfg(test)]
    fn encoded(&self) -> Vec<u8> {
        if self.bulk.is_empty() {
            self.small[..self.small_len].to_vec()
        } else {
            self.bulk
                .iter()
                .flat_map(|slab| slab.data())
                .copied()
                .collect()
        }
    }
    #[cfg(test)]
    fn capacity(&self) -> usize {
        if self.bulk.is_empty() {
            buffers::SMALL_BYTES
        } else {
            self.bulk.len() * buffers::SLAB_BYTES
        }
    }
}

pub(super) async fn response<R: AsyncRead + Unpin>(
    reader: &mut R,
    pool: &buffers::Pool,
) -> Result<(Response, u8)> {
    let mut result = Response {
        small: [0; buffers::SMALL_BYTES],
        small_len: 0,
        bulk: Vec::new(),
        len: 0,
        sql_error: false,
    };
    let mut work = 0usize;
    loop {
        let tag = reader.read_u8().await?;
        let size = reader.read_u32().await? as usize;
        if !(4..=MAX_MESSAGE).contains(&size) {
            bail!("invalid or oversized protocol message");
        }
        if matches!(tag, b'G' | b'H' | b'W') {
            bail!("COPY is unsupported in routed mode");
        }
        if tag == b'Z' && size != 5 {
            bail!("invalid ReadyForQuery transaction status");
        }
        result.reserve_frame(size + 1)?;
        let mut header = [0u8; 5];
        header[0] = tag;
        header[1..].copy_from_slice(&(size as u32).to_be_bytes());
        result.append(&header, pool)?;
        let mut remaining = size - 4;
        while remaining > 0 {
            let target = result.writable(remaining, pool)?;
            let count = target.len();
            reader.read_exact(target).await?;
            remaining -= count;
            work += count;
            // A large buffered packet must not monopolize a runtime worker.
            // The quantum is a work bound, not a latency/performance SLO.
            if work >= 256 * 1024 {
                tokio::task::yield_now().await;
                work = 0;
            }
        }
        result.sql_error |= tag == b'E';
        if tag == b'Z' {
            let status = result.last_byte();
            if !matches!(status, b'I' | b'T' | b'E') {
                bail!("invalid ReadyForQuery transaction status");
            }
            return Ok((result, status));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        pin::Pin,
        task::{Context as TaskContext, Poll},
    };

    struct Fragmented {
        bytes: Vec<u8>,
        maximum: usize,
        fail_after: Option<usize>,
        defer: bool,
    }
    impl AsyncWrite for Fragmented {
        fn poll_write(
            self: Pin<&mut Self>,
            cx: &mut TaskContext<'_>,
            bytes: &[u8],
        ) -> Poll<std::io::Result<usize>> {
            self.poll_write_vectored(cx, &[IoSlice::new(bytes)])
        }
        fn poll_write_vectored(
            mut self: Pin<&mut Self>,
            cx: &mut TaskContext<'_>,
            parts: &[IoSlice<'_>],
        ) -> Poll<std::io::Result<usize>> {
            if self
                .fail_after
                .is_some_and(|limit| self.bytes.len() >= limit)
            {
                return Poll::Ready(Err(Error::from(ErrorKind::BrokenPipe)));
            }
            if self.defer {
                self.defer = false;
                cx.waker().wake_by_ref();
                return Poll::Pending;
            }
            self.defer = true;
            let mut remaining = self.maximum;
            let mut written = 0;
            for part in parts {
                let count = part.len().min(remaining);
                self.bytes.extend_from_slice(&part[..count]);
                written += count;
                remaining -= count;
                if remaining == 0 {
                    break;
                }
            }
            Poll::Ready(Ok(written))
        }
        fn is_write_vectored(&self) -> bool {
            true
        }
        fn poll_flush(self: Pin<&mut Self>, _: &mut TaskContext<'_>) -> Poll<std::io::Result<()>> {
            Poll::Ready(Ok(()))
        }
        fn poll_shutdown(
            self: Pin<&mut Self>,
            _: &mut TaskContext<'_>,
        ) -> Poll<std::io::Result<()>> {
            Poll::Ready(Ok(()))
        }
    }
    fn frame(tag: u8, bytes: &[u8]) -> Vec<u8> {
        let mut result = vec![tag];
        result.extend_from_slice(&((bytes.len() + 4) as u32).to_be_bytes());
        result.extend_from_slice(bytes);
        result
    }

    #[tokio::test]
    async fn vectored_frames_survive_every_header_split_and_empty_body() {
        for maximum in 1..=9 {
            let mut writer = Fragmented {
                bytes: Vec::new(),
                maximum,
                fail_after: None,
                defer: true,
            };
            for (tag, body) in [
                (b'Q', &b"SELECT 1\0"[..]),
                (b'S', &b""[..]),
                (b'D', &[0, 255, 0, 128][..]),
            ] {
                let offset = writer.bytes.len();
                send(&mut writer, tag, body).await.unwrap();
                assert_eq!(&writer.bytes[offset..], frame(tag, body));
            }
        }
    }
    #[tokio::test]
    async fn zero_write_and_disconnect_do_not_duplicate_or_finish_a_frame() {
        let mut zero = Fragmented {
            bytes: Vec::new(),
            maximum: 0,
            fail_after: None,
            defer: true,
        };
        assert!(send(&mut zero, b'Q', b"abc\0").await.is_err());
        assert!(zero.bytes.is_empty());
        for limit in 1..9 {
            let mut writer = Fragmented {
                bytes: Vec::new(),
                maximum: 1,
                fail_after: Some(limit),
                defer: true,
            };
            assert!(send(&mut writer, b'Q', b"abc\0").await.is_err());
            assert_eq!(writer.bytes, frame(b'Q', b"abc\0")[..limit]);
        }
    }
    #[tokio::test]
    async fn private_response_preserves_binary_null_error_and_pipeline_order() {
        let (mut writer, reader) = tokio::io::duplex(16);
        let task = tokio::spawn(async move {
            let mut reader = BufReader::new(reader);
            let first = response(&mut reader, &buffers::Pool::default())
                .await
                .unwrap();
            let second = response(&mut reader, &buffers::Pool::default())
                .await
                .unwrap();
            (first, second)
        });
        let mut first = frame(b'T', b"row description");
        // Two fields: a SQL NULL and two arbitrary binary bytes.
        first.extend(frame(
            b'D',
            &[0, 2, 255, 255, 255, 255, 0, 0, 0, 2, 255, 128],
        ));
        first.extend(frame(b'E', b"SERROR\0C22012\0Mdivision by zero\0\0"));
        writer.write_all(&first).await.unwrap();
        assert!(!task.is_finished(), "response escaped before ReadyForQuery");
        first.extend(frame(b'Z', b"I"));
        writer.write_all(&frame(b'Z', b"I")).await.unwrap();
        let mut second = frame(b'C', b"SELECT 0\0");
        second.extend(frame(b'Z', b"T"));
        writer.write_all(&second).await.unwrap();
        let ((a, sa), (b, sb)) = task.await.unwrap();
        assert_eq!(a.encoded(), first);
        assert!(a.sql_error);
        assert_eq!(sa, b'I');
        assert_eq!(b.encoded(), second);
        assert!(!b.sql_error);
        assert_eq!(sb, b'T');
    }
    #[tokio::test]
    async fn invalid_ready_copy_oversize_and_truncation_discard_private_output() {
        for bytes in [
            frame(b'Z', b""),
            frame(b'Z', b"IX"),
            frame(b'Z', b"X"),
            frame(b'G', b"copy"),
            vec![b'D', 0, 0, 0, 3],
            vec![b'D', 1, 0, 0, 1],
            vec![b'D', 0, 0, 0, 9, b'x'],
        ] {
            let mut reader = &bytes[..];
            assert!(
                response(&mut reader, &buffers::Pool::default())
                    .await
                    .is_err(),
                "accepted {bytes:?}"
            );
        }
    }
    #[tokio::test]
    async fn saturated_bulk_capacity_keeps_small_results_live_and_all_paths_refund() {
        let pool = buffers::Pool::new(buffers::SLAB_BYTES, Metrics::default()).unwrap();
        let mut encoded = frame(b'D', &[7; 300]);
        encoded.extend(frame(b'Z', b"I"));
        let (large, _) = response(&mut encoded.as_slice(), &pool).await.unwrap();
        assert_eq!(pool.snapshot().0, buffers::SLAB_BYTES);
        let error = response(&mut encoded.as_slice(), &pool).await.unwrap_err();
        assert!(error.is::<buffers::Exhausted>());
        assert_eq!(pool.snapshot().0, buffers::SLAB_BYTES);
        let small = frame(b'Z', b"I");
        let (small, _) = response(&mut small.as_slice(), &pool).await.unwrap();
        assert_eq!(pool.snapshot().0, buffers::SLAB_BYTES);
        drop(large);
        drop(small);
        assert_eq!(pool.snapshot().0, 0);
        encoded.truncate(encoded.len() - 8);
        assert!(response(&mut encoded.as_slice(), &pool).await.is_err());
        assert_eq!(pool.snapshot().0, 0);
    }
    #[tokio::test]
    async fn exact_message_and_result_boundaries_keep_capacity_bounded() {
        let (mut writer, reader) = tokio::io::duplex(8192);
        let task = tokio::spawn(async move {
            let mut reader = BufReader::new(reader);
            response(&mut reader, &buffers::Pool::default())
                .await
                .unwrap()
        });
        let body = vec![0; MAX_MESSAGE - 4];
        send(&mut writer, b'D', &body).await.unwrap();
        // First frame = MAX_MESSAGE + 1, second = MAX_MESSAGE - 7;
        // ReadyForQuery = 6. Total is exactly MAX_RESULT.
        send(&mut writer, b'D', &body[..MAX_MESSAGE - 12])
            .await
            .unwrap();
        send(&mut writer, b'Z', b"I").await.unwrap();
        let (result, status) = task.await.unwrap();
        assert_eq!(status, b'I');
        assert_eq!(result.len, MAX_RESULT);
        assert!(result.capacity() <= MAX_RESULT);
        let result = result;
        assert!(result.reserve_frame(1).is_err());
    }
}
