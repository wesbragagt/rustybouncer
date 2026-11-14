//! TLS policy and reload-on-connect PEM credentials (including projected Secrets).
use crate::*;
use std::{
    io::BufReader,
    pin::Pin,
    task::{Context as TaskContext, Poll},
};
use tokio::io::{AsyncRead, ReadBuf};
use tokio_rustls::{
    rustls::{self, pki_types::ServerName},
    TlsAcceptor, TlsConnector,
};

#[derive(Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tls {
    pub ca_file: String,
    pub cert_file: Option<String>,
    pub key_file: Option<String>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerTls {
    pub cert_file: String,
    pub key_file: String,
}
trait Io: AsyncRead + AsyncWrite + Unpin + Send + Sync {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send + Sync> Io for T {}
pub struct Stream {
    io: Box<dyn Io>,
    encrypted: bool,
    #[cfg(test)]
    address: std::net::SocketAddr,
}
impl From<TcpStream> for Stream {
    fn from(stream: TcpStream) -> Self {
        Self {
            #[cfg(test)]
            address: stream.local_addr().unwrap(),
            io: Box::new(stream),
            encrypted: false,
        }
    }
}
impl Stream {
    pub fn encrypted(&self) -> bool {
        self.encrypted
    }
    pub fn healthy_idle(&mut self) -> bool {
        let mut bytes = [0u8; 1];
        let mut buffer = ReadBuf::new(&mut bytes);
        let mut cx = TaskContext::from_waker(std::task::Waker::noop());
        matches!(
            Pin::new(self).poll_read(&mut cx, &mut buffer),
            Poll::Pending
        )
    }
    #[cfg(test)]
    pub fn local_addr(&self) -> std::io::Result<std::net::SocketAddr> {
        Ok(self.address)
    }
}
impl AsyncRead for Stream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut *self.io).poll_read(cx, buffer)
    }
}
impl AsyncWrite for Stream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
        bytes: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut *self.io).poll_write(cx, bytes)
    }
    fn poll_write_vectored(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
        bytes: &[std::io::IoSlice<'_>],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut *self.io).poll_write_vectored(cx, bytes)
    }
    fn is_write_vectored(&self) -> bool {
        self.io.is_write_vectored()
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut TaskContext<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut *self.io).poll_flush(cx)
    }
    fn poll_shutdown(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut *self.io).poll_shutdown(cx)
    }
}
fn certs(path: &str) -> Result<Vec<rustls::pki_types::CertificateDer<'static>>> {
    let data = std::fs::read(path).context("reading TLS certificate")?;
    Ok(rustls_pemfile::certs(&mut BufReader::new(data.as_slice()))
        .collect::<std::io::Result<_>>()?)
}
fn key(path: &str) -> Result<rustls::pki_types::PrivateKeyDer<'static>> {
    let data = std::fs::read(path).context("reading TLS private key")?;
    rustls_pemfile::private_key(&mut BufReader::new(data.as_slice()))?
        .context("missing TLS private key")
}
impl Tls {
    pub fn connector(&self) -> Result<TlsConnector> {
        let mut roots = rustls::RootCertStore::empty();
        for cert in certs(&self.ca_file)? {
            roots.add(cert)?;
        }
        let builder = rustls::ClientConfig::builder().with_root_certificates(roots);
        let config = match (&self.cert_file, &self.key_file) {
            (Some(cert), Some(path)) => builder.with_client_auth_cert(certs(cert)?, key(path)?)?,
            (None, None) => builder.with_no_client_auth(),
            _ => bail!("TLS client certificate and key must be configured together"),
        };
        Ok(TlsConnector::from(Arc::new(config)))
    }
}
impl ServerTls {
    pub fn acceptor(&self) -> Result<TlsAcceptor> {
        let config = rustls::ServerConfig::builder()
            .with_no_client_auth()
            .with_single_cert(certs(&self.cert_file)?, key(&self.key_file)?)?;
        Ok(TlsAcceptor::from(Arc::new(config)))
    }
}
pub async fn backend(mut stream: TcpStream, host: &str, tls: Option<&Tls>) -> Result<Stream> {
    stream.set_nodelay(true)?;
    let Some(tls) = tls else {
        return Ok(stream.into());
    };
    let connector = tls.connector()?;
    stream.write_all(&[0, 0, 0, 8, 4, 210, 22, 47]).await?;
    if stream.read_u8().await? != b'S' {
        bail!("backend refused required TLS")
    }
    let name = ServerName::try_from(host.to_owned())?;
    let secure = connector.connect(name, stream).await?;
    Ok(Stream {
        #[cfg(test)]
        address: secure.get_ref().0.local_addr()?,
        io: Box::new(secure),
        encrypted: true,
    })
}
pub async fn accept(stream: TcpStream, tls: Option<&ServerTls>) -> Result<(Stream, Vec<u8>)> {
    stream.set_nodelay(true)?;
    let mut stream = Stream::from(stream);
    for _ in 0..3 {
        let length = stream.read_u32().await?;
        if !(8..=65536).contains(&length) {
            bail!("invalid startup packet length")
        }
        let mut packet = vec![0; length as usize];
        packet[..4].copy_from_slice(&length.to_be_bytes());
        stream.read_exact(&mut packet[4..]).await?;
        let code = u32::from_be_bytes(packet[4..8].try_into().unwrap());
        match (code, length) {
            (80877103, 8) if !stream.encrypted && tls.is_some() => {
                let acceptor = tls.unwrap().acceptor()?;
                stream.write_all(b"S").await?;
                #[cfg(test)]
                let address = stream.address;
                let secure = acceptor.accept(stream).await?;
                stream = Stream {
                    io: Box::new(secure),
                    encrypted: true,
                    #[cfg(test)]
                    address,
                };
            }
            (80877103 | 80877104, 8) if !stream.encrypted => stream.write_all(b"N").await?,
            // PostgreSQL cancellation carries no SQL and may arrive without TLS.
            (80877102, 16) => return Ok((stream, packet)),
            (196608, _) if packet.last() == Some(&0) => {
                if tls.is_some() && !stream.encrypted {
                    pg_error(&mut stream, "28000", "rustybouncer: TLS required").await?;
                    bail!("plaintext startup rejected");
                }
                return Ok((stream, packet));
            }
            _ => bail!("unsupported PostgreSQL startup packet"),
        }
    }
    bail!("too many encryption negotiation requests")
}
