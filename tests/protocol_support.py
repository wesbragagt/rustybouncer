"""Wire helpers: real PostgreSQL frames, no preparation cache or SQL retries."""
import asyncio
import struct

import failover as f


def frame(tag, body=b''):
    return tag + struct.pack('!I', len(body) + 4) + body


def parse(name, sql, types=()):
    return frame(b'P', name + b'\0' + sql.encode() + b'\0' + struct.pack('!H', len(types))
                 + b''.join(struct.pack('!I', oid) for oid in types))


def bind(statement=b'', values=(), portal=b'', formats=(), results=()):
    body = portal + b'\0' + statement + b'\0' + struct.pack('!H', len(formats))
    body += b''.join(struct.pack('!H', value) for value in formats) + struct.pack('!H', len(values))
    body += b''.join(struct.pack('!i', -1) if value is None else struct.pack('!i', len(value)) + value for value in values)
    return frame(b'B', body + struct.pack('!H', len(results)) + b''.join(struct.pack('!H', value) for value in results))


def execute(portal=b'', maximum=0):
    return frame(b'E', portal + b'\0' + struct.pack('!I', maximum))


def row(body):
    count = struct.unpack_from('!H', body)[0]
    offset, values = 2, []
    for _ in range(count):
        size = struct.unpack_from('!i', body, offset)[0]
        offset += 4
        assert size >= -1 and (size < 0 or offset + size <= len(body))
        values.append(None if size < 0 else body[offset:offset + size])
        offset += max(0, size)
    assert offset == len(body)
    return values


def receive(client, until=b'Z'):
    replies = []
    while True:
        tag = client.exact(1)
        size = struct.unpack('!I', client.exact(4))[0]
        assert 4 <= size <= 16 * 1024 * 1024
        body = client.exact(size - 4)
        replies.append((tag, body))
        if tag == b'E' and f.PgError(body).severity == 'FATAL':
            raise f.PgError(body)
        if tag in until:
            if tag == b'Z':
                assert body in (b'I', b'T', b'E')
                client.transaction_status = body
            return replies


def cycle(client, data):
    client.sock.sendall(data + frame(b'S'))
    return receive(client)


def rows(replies):
    errors = [f.PgError(body) for tag, body in replies if tag == b'E']
    if errors:
        raise errors[0]
    return [row(body) for tag, body in replies if tag == b'D']


class AsyncClient:
    """Actual concurrent sockets allow a 1,000-client barrier without 1,000 threads."""
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, port):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        client = cls(reader, writer)
        params = b'user\0postgres\0database\0postgres\0\0'
        writer.write(struct.pack('!II', len(params) + 8, 196608) + params)
        await writer.drain()
        try:
            await client.receive()
            return client
        except BaseException:
            await client.close()
            raise

    async def receive(self):
        result = []
        while True:
            tag = await self.reader.readexactly(1)
            size = struct.unpack('!I', await self.reader.readexactly(4))[0]
            assert 4 <= size <= 16 * 1024 * 1024
            body = await self.reader.readexactly(size - 4)
            result.append((tag, body))
            if tag == b'E' and f.PgError(body).severity == 'FATAL':
                raise f.PgError(body)
            if tag == b'Z':
                rows(result)
                return result

    async def cycle(self, data):
        self.writer.write(data + frame(b'S'))
        await self.writer.drain()
        return await self.receive()

    async def close(self):
        self.writer.close()
        await asyncio.wait_for(self.writer.wait_closed(), 5)
