"""HTTPS mode: a long-lived self-signed certificate authority (the root a phone installs once)
and a short-lived server certificate signed by it, renewed automatically.

Files in the TLS directory: ca.pem + ca.key, and server.pem (leaf followed by the CA) +
server.key. Keys are PEM PKCS8, unencrypted, mode 0600; certificates 0644; the directory 0700.
Key material is never logged."""
import ipaddress
import logging
import os
import socket
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger(__name__)

CA_DAYS = 3650
LEAF_DAYS = 820   # Apple refuses server certificates valid longer than 825 days
RENEW_BEFORE = timedelta(days=30)
CA_NAME = "SNS Downloader CA"
CA_CERT = "ca.pem"
CA_KEY = "ca.key"
SERVER_CERT = "server.pem"
SERVER_KEY = "server.key"
KEY_SIZE = 2048
CN_MAX = 64   # X.520 upper bound on commonName; the SAN carries the full names

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


@dataclass(frozen=True)
class TlsFiles:
    cert: Path   # server.pem: leaf then CA
    key: Path    # server.key
    ca: Path     # ca.pem


def default_hosts(extra: Iterable[str] = ()) -> list[str]:
    """localhost, the loopback addresses and this machine's hostname, then `extra`;
    lowercased, empty entries dropped, deduplicated in order."""
    out: list[str] = []
    for host in ["localhost", "127.0.0.1", "::1", socket.gethostname(), *extra]:
        host = (host or "").strip().lower()
        if host and host not in out:
            out.append(host)
    return out


def split_hosts(hosts: Iterable[str]) -> tuple[list[str], list[IPAddress]]:
    """(DNS names, IP addresses): an entry that parses as an IP address is one, else a name."""
    names: list[str] = []
    ips: list[IPAddress] = []
    for host in hosts:
        try:
            ips.append(ipaddress.ip_address(host))
        except ValueError:
            names.append(host)
    return names, ips


def load_cert(path: Path) -> Optional[x509.Certificate]:
    """The first certificate in a PEM file; None when missing or unparsable."""
    try:
        return x509.load_pem_x509_certificates(Path(path).read_bytes())[0]
    except (OSError, ValueError, IndexError):
        return None


def san_hosts(cert_path: Path) -> list[str]:
    """DNS names then IP addresses (as strings) from the certificate's SAN; [] when the file
    is missing, unparsable or has no SAN."""
    cert = load_cert(cert_path)
    if cert is None:
        return []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return (list(san.get_values_for_type(x509.DNSName))
            + [str(ip) for ip in san.get_values_for_type(x509.IPAddress)])


def _load_key(path: Path) -> Optional[rsa.RSAPrivateKey]:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    return key if isinstance(key, rsa.RSAPrivateKey) else None


def _same_key(cert: x509.Certificate, key: rsa.RSAPrivateKey) -> bool:
    return cert.public_key().public_numbers() == key.public_key().public_numbers()


def _signed_by(cert: x509.Certificate, ca: x509.Certificate) -> bool:
    if cert.issuer != ca.subject:
        return False
    try:
        ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(),
                               cert.signature_hash_algorithm)
    except Exception:   # wrong key, unsupported algorithm: either way not ours
        return False
    return True


def _write(path: Path, data: bytes, mode: int) -> None:
    """Temp file in the same directory, chmod, fsync, rename into place."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)   # created 0600
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)


def _make_ca(now: datetime) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    start = now - timedelta(days=1)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=True, crl_sign=True,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _make_leaf(hosts: list[str], ca: x509.Certificate, ca_key: rsa.RSAPrivateKey,
               now: datetime) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = _new_key()
    names, ips = split_hosts(hosts)
    start = now - timedelta(days=1)
    ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0][:CN_MAX])]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(n) for n in names] + [x509.IPAddress(ip) for ip in ips]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                     key_encipherment=True, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
                       critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def _host_set(hosts: Iterable[str]) -> set[str]:
    names, ips = split_hosts(hosts)
    return {n.lower() for n in names} | {str(ip) for ip in ips}


def _usable_ca(tls_dir: Path, now: datetime):
    cert, key = load_cert(tls_dir / CA_CERT), _load_key(tls_dir / CA_KEY)
    if cert is None or key is None or not _same_key(cert, key) or cert.not_valid_after_utc <= now:
        return None
    return cert, key


def _leaf_ok(tls_dir: Path, ca: x509.Certificate, hosts: list[str], now: datetime) -> bool:
    cert, key = load_cert(tls_dir / SERVER_CERT), _load_key(tls_dir / SERVER_KEY)
    if cert is None or key is None or not _same_key(cert, key) or not _signed_by(cert, ca):
        return False
    if cert.not_valid_after_utc - now < RENEW_BEFORE:
        return False
    return _host_set(san_hosts(tls_dir / SERVER_CERT)) == _host_set(hosts)


def ensure_certs(tls_dir: Path, hosts: list[str], now: Optional[datetime] = None) -> TlsFiles:
    """Make sure a valid CA and a leaf for exactly `hosts` (CN = first host) exist in
    `tls_dir`, writing only what is missing, damaged, expiring or out of date. OSError
    propagates; an empty host list is a ValueError."""
    hosts = list(dict.fromkeys(h.strip().lower() for h in hosts if h and h.strip()))
    if not hosts:
        raise ValueError("at least one host is needed for the server certificate")
    now = now or datetime.now(timezone.utc)
    tls_dir = Path(tls_dir)
    tls_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(tls_dir, 0o700)
    files = TlsFiles(cert=tls_dir / SERVER_CERT, key=tls_dir / SERVER_KEY, ca=tls_dir / CA_CERT)

    usable = _usable_ca(tls_dir, now)
    if usable is None:
        existed = (tls_dir / CA_CERT).exists() or (tls_dir / CA_KEY).exists()
        ca, ca_key = _make_ca(now)
        _write(tls_dir / CA_KEY, _key_pem(ca_key), 0o600)
        _write(tls_dir / CA_CERT, _cert_pem(ca), 0o644)
        if existed:
            log.warning("new certificate authority written; phones must install /cert.pem again")
        else:
            log.info("certificate authority written to %s", tls_dir / CA_CERT)
    else:
        ca, ca_key = usable
        if _leaf_ok(tls_dir, ca, hosts, now):
            return files

    leaf, leaf_key = _make_leaf(hosts, ca, ca_key, now)
    _write(tls_dir / SERVER_KEY, _key_pem(leaf_key), 0o600)
    _write(tls_dir / SERVER_CERT, _cert_pem(leaf) + _cert_pem(ca), 0o644)
    log.info("server certificate written for %s", ", ".join(hosts))
    return files
