import ipaddress
import logging
import os
import socket
import stat
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app import tls

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
HOSTS = ["localhost", "127.0.0.1", "::1", "nas.local", "192.168.1.10"]


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _snapshot(path):
    st = os.stat(path)
    return st.st_mtime_ns, path.read_bytes()


def _all(d):
    return {name: _snapshot(d / name) for name in (tls.CA_CERT, tls.CA_KEY, tls.SERVER_CERT, tls.SERVER_KEY)}


def _leaf(d):
    return x509.load_pem_x509_certificates((d / tls.SERVER_CERT).read_bytes())[0]


def _ca(d):
    return x509.load_pem_x509_certificate((d / tls.CA_CERT).read_bytes())


def _signed_by(cert, ca):
    ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(),
                           cert.signature_hash_algorithm)
    return True


def test_constants():
    assert tls.CA_DAYS == 3650
    assert tls.LEAF_DAYS == 820
    assert tls.RENEW_BEFORE == timedelta(days=30)
    assert tls.CA_NAME == "SNS Downloader CA"
    assert (tls.CA_CERT, tls.CA_KEY, tls.SERVER_CERT, tls.SERVER_KEY) == (
        "ca.pem", "ca.key", "server.pem", "server.key")


def test_writes_four_files_with_modes(tmp_path):
    d = tmp_path / "config" / "tls"
    files = tls.ensure_certs(d, HOSTS, now=NOW)
    assert files == tls.TlsFiles(cert=d / "server.pem", key=d / "server.key", ca=d / "ca.pem")
    assert _mode(d) == 0o700
    assert _mode(d / "ca.key") == 0o600
    assert _mode(d / "server.key") == 0o600
    assert _mode(d / "ca.pem") == 0o644
    assert _mode(d / "server.pem") == 0o644
    assert sorted(p.name for p in d.iterdir()) == ["ca.key", "ca.pem", "server.key", "server.pem"]
    for key in ("ca.key", "server.key"):
        data = (d / key).read_bytes()
        assert data.startswith(b"-----BEGIN PRIVATE KEY-----")


def test_ca_properties(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    ca = _ca(tmp_path)
    cn = ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "SNS Downloader CA"
    assert ca.issuer == ca.subject
    bc = ca.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca is True and bc.value.path_length == 0
    ku = ca.extensions.get_extension_for_class(x509.KeyUsage)
    assert ku.critical and ku.value.key_cert_sign and ku.value.crl_sign and ku.value.digital_signature
    ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    assert ca.public_key().key_size == 2048
    assert ca.signature_hash_algorithm.name == "sha256"
    assert ca.not_valid_before_utc == NOW - timedelta(days=1)
    assert ca.not_valid_after_utc == NOW - timedelta(days=1) + timedelta(days=3650)
    assert _signed_by(ca, ca)


def test_leaf_properties(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    ca, leaf = _ca(tmp_path), _leaf(tmp_path)
    assert leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "localhost"
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["localhost", "nas.local"]
    assert san.get_values_for_type(x509.IPAddress) == [
        ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1"), ipaddress.ip_address("192.168.1.10")]
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in list(eku)
    bc = leaf.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca is False
    ku = leaf.extensions.get_extension_for_class(x509.KeyUsage)
    assert ku.critical and ku.value.digital_signature and ku.value.key_encipherment
    aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    assert aki.key_identifier == ski.digest
    assert leaf.issuer == ca.subject
    assert leaf.signature_hash_algorithm.name == "sha256"
    assert leaf.public_key().key_size == 2048
    assert _signed_by(leaf, ca)
    assert leaf.not_valid_before_utc == NOW - timedelta(days=1)
    assert leaf.not_valid_after_utc == NOW - timedelta(days=1) + timedelta(days=820)


def test_server_pem_is_leaf_then_ca(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    chain = x509.load_pem_x509_certificates((tmp_path / "server.pem").read_bytes())
    assert len(chain) == 2
    assert chain[0].subject != chain[0].issuer
    assert chain[1] == _ca(tmp_path)


def test_second_call_reuses(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    before = _all(tmp_path)
    tls.ensure_certs(tmp_path, list(HOSTS), now=NOW + timedelta(days=1))
    assert _all(tmp_path) == before


def test_same_hosts_other_order_and_case_reuses(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    before = _all(tmp_path)
    tls.ensure_certs(tmp_path, ["NAS.local", "192.168.1.10", "::1", "127.0.0.1", "localhost"], now=NOW)
    assert _all(tmp_path) == before


def test_hosts_changed_new_leaf_same_ca(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    before = _all(tmp_path)
    tls.ensure_certs(tmp_path, HOSTS + ["10.0.0.5"], now=NOW)
    after = _all(tmp_path)
    assert after["ca.pem"] == before["ca.pem"] and after["ca.key"] == before["ca.key"]
    assert after["server.pem"][1] != before["server.pem"][1]
    assert after["server.key"][1] != before["server.key"][1]
    assert "10.0.0.5" in tls.san_hosts(tmp_path / "server.pem")
    assert _signed_by(_leaf(tmp_path), _ca(tmp_path))


def test_leaf_near_expiry_renewed_same_ca(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    before = _all(tmp_path)
    later = NOW + timedelta(days=800)
    tls.ensure_certs(tmp_path, HOSTS, now=later)
    after = _all(tmp_path)
    assert after["ca.pem"] == before["ca.pem"]
    assert after["server.pem"][1] != before["server.pem"][1]
    assert _leaf(tmp_path).not_valid_after_utc == later - timedelta(days=1) + timedelta(days=820)


def test_leaf_not_yet_near_expiry_reused(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    before = _all(tmp_path)
    tls.ensure_certs(tmp_path, HOSTS, now=NOW + timedelta(days=780))   # 39 days left
    assert _all(tmp_path) == before


def test_leaf_clamped_to_ca_expiry_and_reused(tmp_path, caplog):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    ca_end = _ca(tmp_path).not_valid_after_utc
    later = ca_end - timedelta(days=400)   # the first leaf has long expired; 400 days of CA left
    caplog.set_level(logging.INFO, logger="app.tls")
    tls.ensure_certs(tmp_path, HOSTS, now=later)
    leaf = _leaf(tmp_path)
    assert leaf.not_valid_after_utc == ca_end
    assert _signed_by(leaf, _ca(tmp_path))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert (f"certificate authority expires on {ca_end.date().isoformat()}; phones will need to "
            "install /cert.pem again after a new one is written") in warnings
    before = _all(tmp_path)
    tls.ensure_certs(tmp_path, HOSTS, now=later + timedelta(days=1))
    assert _all(tmp_path) == before


def test_unclamped_leaf_no_ca_warning(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="app.tls")
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    assert not any("expires on" in r.getMessage() for r in caplog.records)


def test_ca_expired_new_ca_and_leaf(tmp_path, caplog):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    before = _all(tmp_path)
    caplog.set_level(logging.INFO, logger="app.tls")
    later = NOW + timedelta(days=3700)
    tls.ensure_certs(tmp_path, HOSTS, now=later)
    after = _all(tmp_path)
    for name in before:
        assert after[name][1] != before[name][1]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("new certificate authority written; phones must install /cert.pem again" in r.getMessage()
               for r in warnings)
    assert _signed_by(_leaf(tmp_path), _ca(tmp_path))


def test_first_ca_no_warning(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="app.tls")
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_corrupt_ca_key_regenerates_both(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    before = _all(tmp_path)
    (tmp_path / "ca.key").write_bytes(b"garbage")
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    after = _all(tmp_path)
    for name in ("ca.pem", "server.pem", "server.key"):
        assert after[name][1] != before[name][1]
    assert after["ca.key"][1].startswith(b"-----BEGIN PRIVATE KEY-----")
    assert _signed_by(_leaf(tmp_path), _ca(tmp_path))


def test_corrupt_server_pem_regenerates_leaf(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    before = _all(tmp_path)
    (tmp_path / "server.pem").write_bytes(b"not a certificate")
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    after = _all(tmp_path)
    assert after["ca.pem"] == before["ca.pem"] and after["ca.key"] == before["ca.key"]
    assert after["server.key"][1] != before["server.key"][1]
    assert len(x509.load_pem_x509_certificates(after["server.pem"][1])) == 2
    assert _signed_by(_leaf(tmp_path), _ca(tmp_path))


def test_leaf_from_other_ca_regenerated(tmp_path):
    other = tmp_path / "other"
    mine = tmp_path / "mine"
    tls.ensure_certs(other, HOSTS, now=NOW)
    tls.ensure_certs(mine, HOSTS, now=NOW)
    (mine / "server.pem").write_bytes((other / "server.pem").read_bytes())
    (mine / "server.key").write_bytes((other / "server.key").read_bytes())
    tls.ensure_certs(mine, HOSTS, now=NOW)
    assert _signed_by(_leaf(mine), _ca(mine))


def test_corrupt_server_key_regenerates_leaf(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    before = _all(tmp_path)
    (tmp_path / "server.key").write_bytes(b"garbage")
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    after = _all(tmp_path)
    assert after["ca.pem"] == before["ca.pem"]
    assert after["server.pem"][1] != before["server.pem"][1]
    assert after["server.key"][1].startswith(b"-----BEGIN PRIVATE KEY-----")


def test_key_bytes_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    tls.ensure_certs(tmp_path, HOSTS + ["x.lan"], now=NOW)
    text = caplog.text
    assert "PRIVATE KEY" not in text
    for key in ("ca.key", "server.key"):
        body = (tmp_path / key).read_text().splitlines()[1]
        assert body not in text


def test_empty_hosts_rejected(tmp_path):
    with pytest.raises(ValueError):
        tls.ensure_certs(tmp_path, [], now=NOW)


def test_default_hosts(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "MyBox")
    assert tls.default_hosts() == ["localhost", "127.0.0.1", "::1", "mybox"]
    assert tls.default_hosts(["NAS.local", "", "localhost", "192.168.1.10", "nas.local", "mybox"]) == [
        "localhost", "127.0.0.1", "::1", "mybox", "nas.local", "192.168.1.10"]


def test_default_hosts_empty_hostname(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert tls.default_hosts(["a.lan"]) == ["localhost", "127.0.0.1", "::1", "a.lan"]


def test_split_hosts():
    names, ips = tls.split_hosts(["localhost", "::1", "nas.local", "fe80::1", "10.0.0.1"])
    assert names == ["localhost", "nas.local"]
    assert ips == [ipaddress.IPv6Address("::1"), ipaddress.IPv6Address("fe80::1"),
                   ipaddress.IPv4Address("10.0.0.1")]


def test_san_hosts_round_trip(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    assert tls.san_hosts(tmp_path / "server.pem") == ["localhost", "nas.local", "127.0.0.1", "::1",
                                                      "192.168.1.10"]


def test_san_hosts_missing_or_without_san(tmp_path):
    assert tls.san_hosts(tmp_path / "nope.pem") == []
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    assert tls.san_hosts(tmp_path / "ca.pem") == []
    (tmp_path / "bad.pem").write_bytes(b"junk")
    assert tls.san_hosts(tmp_path / "bad.pem") == []


def test_load_cert(tmp_path):
    assert tls.load_cert(tmp_path / "missing.pem") is None
    (tmp_path / "bad.pem").write_bytes(b"junk")
    assert tls.load_cert(tmp_path / "bad.pem") is None
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    assert tls.load_cert(tmp_path / "server.pem") == _leaf(tmp_path)


def test_certs_dated_in_future_replaced(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW)
    before = _all(tmp_path)
    tls.ensure_certs(tmp_path, HOSTS, now=NOW - timedelta(days=5))
    after = _all(tmp_path)
    for name in before:
        assert after[name][1] != before[name][1]
    assert _signed_by(_leaf(tmp_path), _ca(tmp_path))


def test_naive_now_treated_as_utc(tmp_path):
    tls.ensure_certs(tmp_path, HOSTS, now=NOW.replace(tzinfo=None))
    assert _leaf(tmp_path).not_valid_before_utc == NOW - timedelta(days=1)
    before = _all(tmp_path)
    tls.ensure_certs(tmp_path, HOSTS, now=NOW.replace(tzinfo=None))
    assert _all(tmp_path) == before


def test_scoped_ipv6_scope_dropped_and_reused(tmp_path):
    hosts = ["localhost", "fe80::1%eth0"]
    tls.ensure_certs(tmp_path, hosts, now=NOW)
    san = _leaf(tmp_path).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("fe80::1")]
    before = _all(tmp_path)
    tls.ensure_certs(tmp_path, hosts, now=NOW)
    assert _all(tmp_path) == before
