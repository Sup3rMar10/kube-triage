import pytest

from kubetriage.quantity import cpu_millicores, fmt_bytes, fmt_cpu, mem_bytes, parse_quantity


@pytest.mark.parametrize("text,expected", [("500m", 0.5), ("2", 2.0), ("1e3", 1000.0), ("100k", 1e5), ("1.5", 1.5)])
def test_parse_quantity(text, expected):
    assert parse_quantity(text) == pytest.approx(expected)


def test_cpu_and_memory_units():
    assert cpu_millicores("250m") == 250
    assert cpu_millicores("2") == 2000
    assert mem_bytes("128Mi") == 128 * 2**20
    assert mem_bytes("1Gi") == 2**30
    assert mem_bytes("1.5Gi") == int(1.5 * 2**30)
    assert mem_bytes("500M") == 500_000_000          # decimal M, not Mi
    assert mem_bytes("1Ei") == 2**60


@pytest.mark.parametrize("bad", ["", "abc", "12Zi", "1..2", "Mi"])
def test_malformed_quantities_raise(bad):
    with pytest.raises(ValueError):
        parse_quantity(bad)


def test_formatting():
    assert fmt_bytes(256 * 2**20) == "256Mi"
    assert fmt_bytes(2 * 2**30) == "2Gi"
    assert fmt_bytes(1536 * 2**20) == "1.5Gi"
    assert fmt_bytes(500) == "500B"
    assert fmt_cpu(500) == "500m"
    assert fmt_cpu(1000) == "1 CPU"
    assert fmt_cpu(4000) == "4 CPUs"
