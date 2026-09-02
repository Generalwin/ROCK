import pytest

from rock.utils.format import (
    convert_to_gb,
    megabytes_to_size,
    normalize_memory_to_k8s,
    parse_size_to_bytes,
    parse_size_to_mb,
)


def test_bytes_without_unit():
    assert parse_size_to_bytes("100") == 100
    assert parse_size_to_bytes("0") == 0
    assert parse_size_to_bytes("1024") == 1024


def test_bytes_with_b_unit():
    assert parse_size_to_bytes("100b") == 100
    assert parse_size_to_bytes("100B") == 100
    assert parse_size_to_bytes("0b") == 0


def test_kilobytes():
    assert parse_size_to_bytes("1k") == 1024
    assert parse_size_to_bytes("1K") == 1024
    assert parse_size_to_bytes("1kb") == 1024
    assert parse_size_to_bytes("1KB") == 1024
    assert parse_size_to_bytes("1Ki") == 1024
    assert parse_size_to_bytes("2k") == 2048


def test_megabytes():
    assert parse_size_to_bytes("1m") == 1024**2
    assert parse_size_to_bytes("1M") == 1024**2
    assert parse_size_to_bytes("1mb") == 1024**2
    assert parse_size_to_bytes("1MB") == 1024**2
    assert parse_size_to_bytes("1Mi") == 1024**2
    assert parse_size_to_bytes("2m") == 2 * 1024**2


def test_gigabytes():
    assert parse_size_to_bytes("1g") == 1024**3
    assert parse_size_to_bytes("1G") == 1024**3
    assert parse_size_to_bytes("1gb") == 1024**3
    assert parse_size_to_bytes("1GB") == 1024**3
    assert parse_size_to_bytes("1Gi") == 1024**3
    assert parse_size_to_bytes("100Gi") == 100 * 1024**3
    assert parse_size_to_bytes("2g") == 2 * 1024**3


def test_terabytes():
    assert parse_size_to_bytes("1t") == 1024**4
    assert parse_size_to_bytes("1T") == 1024**4
    assert parse_size_to_bytes("1tb") == 1024**4
    assert parse_size_to_bytes("1TB") == 1024**4
    assert parse_size_to_bytes("1Ti") == 1024**4


def test_decimal_values():
    assert parse_size_to_bytes("1.5k") == int(1.5 * 1024)
    assert parse_size_to_bytes("2.5m") == int(2.5 * 1024**2)
    assert parse_size_to_bytes("0.5g") == int(0.5 * 1024**3)


def test_whitespace_handling():
    assert parse_size_to_bytes(" 100 ") == 100
    assert parse_size_to_bytes(" 1k ") == 1024
    assert parse_size_to_bytes("1 k") == 1024
    assert parse_size_to_bytes(" 1 mb ") == 1024**2


def test_invalid_format():
    with pytest.raises(ValueError, match="Invalid memory size format"):
        parse_size_to_bytes("abc")
    with pytest.raises(ValueError, match="Invalid memory size format"):
        parse_size_to_bytes("1.2.3k")
    with pytest.raises(ValueError, match="Invalid memory size format"):
        parse_size_to_bytes("")


def test_unknown_unit():
    with pytest.raises(ValueError, match="Unknown memory unit"):
        parse_size_to_bytes("100x")
    with pytest.raises(ValueError, match="Unknown memory unit"):
        parse_size_to_bytes("100pb")


def test_edge_cases():
    assert parse_size_to_bytes("0.0") == 0
    assert parse_size_to_bytes("0.0k") == 0
    assert parse_size_to_bytes("1000") == 1000


@pytest.mark.parametrize(
    ("size_str", "expected"),
    [("8g", 8192), ("4096m", 4096), ("2048", 2048), ("", 0), ("4G", 4096), ("50G", 51200), (None, 0)],
)
def test_parse_size_to_mb(size_str, expected):
    assert parse_size_to_mb(size_str) == expected


@pytest.mark.parametrize(
    ("megabytes", "expected"),
    [(16384, "16g"), (1536, "1536m")],
)
def test_megabytes_to_size(megabytes, expected):
    assert megabytes_to_size(megabytes) == expected


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        ("8g", "8.00Gi"),
        ("2048m", "2048.00Mi"),
        ("1.5G", "1.50Gi"),
        ("2k", "2.00Ki"),
        ("1t", "1.00Ti"),
        ("8Gi", "8Gi"),
        ("1073741824", "1024.00Mi"),
        ("abc", "abc"),
        ("100x", "100x"),
    ],
)
def test_normalize_memory_to_k8s(memory, expected):
    assert normalize_memory_to_k8s(memory) == expected


def test_convert_to_gb_from_bytes():
    assert convert_to_gb("1073741824") == "1.00g"
    assert convert_to_gb("536870912") == "0.50g"


def test_convert_to_gb_from_kilobytes():
    assert convert_to_gb("1048576k") == "1.00g"
    assert convert_to_gb("2097152kb") == "2.00g"


def test_convert_to_gb_from_megabytes():
    assert convert_to_gb("1024m") == "1.00g"
    assert convert_to_gb("512mb") == "0.50g"
    assert convert_to_gb("2048M") == "2.00g"


def test_convert_to_gb_from_gigabytes():
    assert convert_to_gb("1g") == "1.00g"
    assert convert_to_gb("2.5gb") == "2.50g"
    assert convert_to_gb("0.25G") == "0.25g"


def test_convert_to_gb_from_terabytes():
    assert convert_to_gb("1t") == "1024.00g"
    assert convert_to_gb("0.5tb") == "512.00g"


def test_convert_to_gb_small_values():
    assert convert_to_gb("100m") == "0.10g"
    assert convert_to_gb("1m") == "0.00g"


def test_convert_to_gb_with_whitespace():
    assert convert_to_gb(" 1024m ") == "1.00g"
    assert convert_to_gb("2 g") == "2.00g"
