"""Encode source URLs for HTTP while preserving the original URL in exported data."""
from urllib.parse import quote, urlsplit, urlunsplit


def transport_url(url):
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise ValueError('Credential-bearing source URL is not supported')
    host = (parts.hostname or '').encode('idna').decode('ascii')
    if ':' in host:
        host = '[' + host + ']'
    if parts.port:
        host += ':' + str(parts.port)
    return urlunsplit((parts.scheme, host,
                       quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                       quote(parts.query, safe="%&=+;,:@/?$!()'*~-._"), ''))
