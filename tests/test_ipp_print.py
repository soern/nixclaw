"""Unit tests for the print skill's IPP client (skills/print/scripts/ipp_print.py).

These are pure offline tests - no printer and no network involved. They pin down
the bugs that previously made duplex/monochrome printing fail silently.
"""

import http.client
import importlib.util
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "skills" / "print" / "scripts" / "ipp_print.py"
)
_spec = importlib.util.spec_from_file_location("ipp_print", MODULE_PATH)
assert _spec and _spec.loader
ipp = importlib.util.module_from_spec(_spec)
sys.modules["ipp_print"] = ipp     # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(ipp)


# --------------------------------------------------------------------------
# Constants that were wrong before
# --------------------------------------------------------------------------


def test_print_quality_enums_are_rfc_values():
    """RFC 8011: 3=draft, 4=normal, 5=high. The old script used 3/2/1."""
    assert ipp.QUALITY_ENUM == {"draft": 3, "normal": 4, "high": 5}


def test_legacy_color_aliases_map_to_valid_ipp_keyword():
    """'mono' is not a valid IPP keyword; it must become 'monochrome'."""
    for alias in ("mono", "bw", "sw", "gray", "grey"):
        assert ipp.COLOR_ALIASES[alias] == "monochrome"


def test_ipp_status_ranges_are_not_inverted():
    """0x04xx = client errors, 0x05xx = server errors (they were swapped once)."""
    assert ipp.IPP_STATUS[0x040A] == "client-error-document-format-not-supported"
    assert ipp.IPP_STATUS[0x040B] == "client-error-attributes-or-values-not-supported"
    assert ipp.IPP_STATUS[0x0500] == "server-error-internal-error"
    assert ipp.IPP_STATUS[0x0000] == "successful-ok"


def test_duplex_attributes_have_explicit_value_tags():
    """Attributes silently dropped by pyipp must be present with correct tags."""
    assert ipp.ATTR_TAGS["sides"] == ipp.TAG_KEYWORD
    assert ipp.ATTR_TAGS["print-color-mode"] == ipp.TAG_KEYWORD
    assert ipp.ATTR_TAGS["print-quality"] == ipp.TAG_ENUM
    assert ipp.ATTR_TAGS["printer-resolution"] == ipp.TAG_RESOLUTION


# --------------------------------------------------------------------------
# Encoder
# --------------------------------------------------------------------------


def test_encode_attr_keyword_bytes():
    out = ipp._encode_attr("sides", "two-sided-long-edge")
    assert out[0] == ipp.TAG_KEYWORD
    assert struct.unpack(">H", out[1:3])[0] == len("sides")
    assert out[3:8] == b"sides"
    assert struct.unpack(">H", out[8:10])[0] == len("two-sided-long-edge")
    assert out[10:] == b"two-sided-long-edge"


def test_encode_attr_enum_is_four_bytes():
    out = ipp._encode_attr("print-quality", 3)
    assert out[0] == ipp.TAG_ENUM
    assert struct.unpack(">H", out[-6:-4])[0] == 4
    assert struct.unpack(">i", out[-4:])[0] == 3


def test_encode_attr_set_uses_zero_length_name_for_extra_values():
    out = ipp._encode_attr("requested-attributes", ["job-id", "job-state"])
    second = out.index(b"job-state") - 4
    assert struct.unpack(">H", out[second : second + 2])[0] == 0


def test_encode_attr_rejects_unknown_attribute():
    """Never guess a tag - a wrong guess is what made attributes disappear."""
    with pytest.raises(ValueError, match="unknown IPP attribute"):
        ipp._encode_attr("totally-made-up-attribute", "x")


def test_build_request_structure():
    payload = ipp.build_request(
        ipp.OP_PRINT_JOB,
        [("attributes-charset", "utf-8"), ("printer-uri", "ipp://p/ipp/print")],
        [("sides", "two-sided-long-edge")],
        data=b"RASTERDATA",
    )
    assert payload[0:2] == b"\x01\x01"                       # version 1.1
    assert struct.unpack(">H", payload[2:4])[0] == ipp.OP_PRINT_JOB
    assert payload[8] == ipp.TAG_OPERATION_ATTRS
    assert ipp.TAG_JOB_ATTRS in payload
    assert payload.endswith(b"\x03RASTERDATA")               # end-of-attrs + document


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _response(status: int, groups: list) -> bytes:
    out = struct.pack(">BBHi", 1, 1, status, 1)
    for tag, attrs in groups:
        out += struct.pack(">B", tag)
        for name, (vtag, raw) in attrs:
            out += struct.pack(">B", vtag)
            out += struct.pack(">H", len(name)) + name.encode()
            out += struct.pack(">H", len(raw)) + raw
    return out + struct.pack(">B", ipp.TAG_END_OF_ATTRS)


def test_parse_simple_job_response():
    body = _response(0x0000, [
        (ipp.TAG_JOB_ATTRS, [
            ("job-id", (ipp.TAG_INTEGER, struct.pack(">i", 641))),
            ("job-state", (ipp.TAG_ENUM, struct.pack(">i", 9))),
        ]),
    ])
    resp = ipp.parse_response(body)
    assert resp.ok and resp.status_name == "successful-ok"
    job = resp.merged(ipp.TAG_JOB_ATTRS)
    assert job["job-id"] == 641 and job["job-state"] == 9


def test_parse_set_of_values():
    body = struct.pack(">BBHi", 1, 1, 0, 1)
    body += struct.pack(">B", ipp.TAG_PRINTER_ATTRS)
    body += struct.pack(">B", ipp.TAG_KEYWORD)
    body += struct.pack(">H", len("sides-supported")) + b"sides-supported"
    body += struct.pack(">H", len("one-sided")) + b"one-sided"
    body += struct.pack(">B", ipp.TAG_KEYWORD) + struct.pack(">H", 0)
    body += struct.pack(">H", len("two-sided-long-edge")) + b"two-sided-long-edge"
    body += struct.pack(">B", ipp.TAG_END_OF_ATTRS)
    attrs = ipp.parse_response(body).merged(ipp.TAG_PRINTER_ATTRS)
    assert attrs["sides-supported"] == ["one-sided", "two-sided-long-edge"]


def test_unsupported_attributes_group_is_captured():
    """The printer telling us it threw a setting away must be visible."""
    body = _response(0x0001, [
        (ipp.TAG_UNSUPPORTED_ATTRS, [
            ("print-color-mode", (ipp.TAG_KEYWORD, b"mono")),
        ]),
    ])
    resp = ipp.parse_response(body)
    assert resp.unsupported() == {"print-color-mode": "mono"}
    assert resp.substituted is True


def test_collection_members_do_not_corrupt_previous_attribute():
    body = struct.pack(">BBHi", 1, 1, 0, 1)
    body += struct.pack(">B", ipp.TAG_PRINTER_ATTRS)
    body += struct.pack(">B", ipp.TAG_KEYWORD)
    body += struct.pack(">H", len("media-default")) + b"media-default"
    body += struct.pack(">H", len("iso_a4")) + b"iso_a4"
    body += struct.pack(">B", ipp.TAG_BEG_COLLECTION)
    body += struct.pack(">H", len("media-col")) + b"media-col"
    body += struct.pack(">H", 0)
    body += struct.pack(">B", ipp.TAG_MEMBER_NAME) + struct.pack(">H", 0)
    body += struct.pack(">H", len("media-type")) + b"media-type"
    body += struct.pack(">B", ipp.TAG_KEYWORD) + struct.pack(">H", 0)
    body += struct.pack(">H", len("stationery")) + b"stationery"
    body += struct.pack(">B", ipp.TAG_END_COLLECTION) + struct.pack(">H", 0)
    body += struct.pack(">H", 0)
    body += struct.pack(">B", ipp.TAG_END_OF_ATTRS)
    attrs = ipp.parse_response(body).merged(ipp.TAG_PRINTER_ATTRS)
    assert attrs["media-default"] == "iso_a4"       # not polluted by collection members
    assert attrs["media-col"] == "<collection>"


def test_truncated_response_raises_value_error_not_struct_error():
    body = _response(0x0000, [
        (ipp.TAG_JOB_ATTRS, [("job-id", (ipp.TAG_INTEGER, struct.pack(">i", 1)))]),
    ])
    with pytest.raises(ValueError):
        ipp.parse_response(body[:-6])


def test_too_short_response_raises_value_error():
    with pytest.raises(ValueError, match="too short"):
        ipp.parse_response(b"\x01\x01")


# --------------------------------------------------------------------------
# URI handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize("uri,expected", [
    ("ipp://172.17.15.237:631/ipp/print", "http://172.17.15.237:631/ipp/print"),
    ("ipp://172.17.15.237/ipp/print", "http://172.17.15.237:631/ipp/print"),
    ("ipp://printer.local", "http://printer.local:631/ipp/print"),
    ("ipps://printer.local/ipp/print", "https://printer.local:443/ipp/print"),
])
def test_http_url_conversion(uri, expected):
    assert ipp.http_url(uri) == expected


# --------------------------------------------------------------------------
# CLI option normalisation (the --duplex alias-ordering bug)
# --------------------------------------------------------------------------


def _cli(argv, monkeypatch):
    """Run main() with a stubbed subcommand, returning (exit_code, args)."""
    captured = {}

    def fake(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(ipp, "cmd_validate", fake)
    monkeypatch.setattr(ipp.sys, "argv", ["ipp_print.py"] + argv)
    # rebuild parser defaults each call
    code = ipp.main()
    return code, captured.get("args")


BASE = ["validate", "ipp://p/ipp/print", "/tmp/x.pdf"]


def test_duplex_flag_sets_long_edge(monkeypatch):
    code, args = _cli(BASE + ["--duplex"], monkeypatch)
    assert code == 0 and args.sides == "two-sided-long-edge"


def test_duplex_with_alias_sides_is_not_silently_dropped(monkeypatch):
    """`--sides=simplex --duplex` must end up duplex, not one-sided."""
    code, args = _cli(BASE + ["--sides=simplex", "--duplex"], monkeypatch)
    assert code == 0 and args.sides == "two-sided-long-edge"


def test_duplex_conflicting_with_short_edge_is_an_error(monkeypatch):
    code, _ = _cli(BASE + ["--sides=two-sided-short-edge", "--duplex"], monkeypatch)
    assert code == 1


def test_legacy_mono_alias_normalised(monkeypatch):
    code, args = _cli(BASE + ["--color=mono"], monkeypatch)
    assert code == 0 and args.color == "monochrome"


def test_invalid_sides_rejected(monkeypatch):
    code, _ = _cli(BASE + ["--sides=both"], monkeypatch)
    assert code == 1


def test_zero_copies_rejected(monkeypatch):
    code, _ = _cli(BASE + ["--copies=0"], monkeypatch)
    assert code == 1


@pytest.mark.parametrize("alias,expected", [
    ("1", "one-sided"),
    ("simplex", "one-sided"),
    ("2", "two-sided-long-edge"),
    ("duplex", "two-sided-long-edge"),
    ("long", "two-sided-long-edge"),
    ("short", "two-sided-short-edge"),
    ("tumble", "two-sided-short-edge"),
])
def test_sides_alias_matrix(alias, expected, monkeypatch):
    code, args = _cli(BASE + [f"--sides={alias}"], monkeypatch)
    assert code == 0 and args.sides == expected


# --------------------------------------------------------------------------
# Consent contract + one-job printing (incident 2026-10-06: printed without
# confirmation; 9 copies became 21 jobs). No network: ipp_request is faked.
# --------------------------------------------------------------------------

CRC = "1a2b3c4d"
URI = "ipp://printer.invalid:631/ipp/print"
FIDELITY = ipp._encode_attr("ipp-attribute-fidelity", True)
REAL_IPP_REQUEST = ipp.ipp_request


def _has_copies_attr(payload: bytes) -> bool:
    return bytes([ipp.TAG_INTEGER]) + struct.pack(">H", 6) + b"copies" in payload


def _pwg_header(height=4, bytes_per_line=8, bpp=8, color_space=18, total_pages=0):
    h = bytearray(ipp.PWG_HEADER_SIZE)
    h[0:9] = b"PwgRaster"
    for off, val in ((376, height), (384, 8), (388, bpp), (392, bytes_per_line),
                     (400, color_space), (452, total_pages)):
        struct.pack_into(">I", h, off, val)
    return bytes(h)


def _pwg_page(marker: int, height=4, bytes_per_line=8, **kw) -> bytes:
    """One page: first line literal (unique marker bytes), rest repeated white."""
    header = _pwg_header(height=height, bytes_per_line=bytes_per_line, **kw)
    first = bytes([0, 257 - bytes_per_line]) + bytes([marker]) * bytes_per_line
    rest = bytes([height - 2, 128])                    # 128 = clear line to white
    return header + first + rest


def _pwg(pages: int, **kw) -> bytes:
    return ipp.PWG_SYNC + b"".join(_pwg_page(i + 1, **kw) for i in range(pages))


class FakePrinter:
    """Stands in for ipp_request; records every request by operation."""

    def __init__(self, caps=None, print_status=0x0000, unsupported=None,
                 fail_print=False):
        self.caps = {
            "document-format-supported": ["image/jpeg", "image/pwg-raster"],
            "sides-supported": ["one-sided", "two-sided-long-edge", "two-sided-short-edge"],
            "print-color-mode-supported": ["auto", "color", "monochrome"],
            "print-quality-supported": [3, 4, 5],
            "media-supported": ["iso_a4_210x297mm"],
            "copies-supported": "1-99",
            "printer-state": 3,
        }
        self.caps.update(caps or {})
        self.print_status = print_status
        self.unsupported = unsupported
        self.fail_print = fail_print
        self.requests = []

    def ops(self):
        return [struct.unpack(">H", p[2:4])[0] for p in self.requests]

    def payloads(self, op):
        return [p for p in self.requests if struct.unpack(">H", p[2:4])[0] == op]

    def __call__(self, uri, payload, timeout=60):
        self.requests.append(payload)
        op = struct.unpack(">H", payload[2:4])[0]
        if op == ipp.OP_GET_PRINTER_ATTRIBUTES:
            return ipp.IppResponse(0, [(ipp.TAG_PRINTER_ATTRS, dict(self.caps))])
        if op == ipp.OP_PRINT_JOB:
            if self.fail_print:
                raise RuntimeError("Cannot reach printer: timed out")
            groups = [(ipp.TAG_JOB_ATTRS, {"job-id": 641, "job-state": 3})]
            if self.unsupported:
                groups.append((ipp.TAG_UNSUPPORTED_ATTRS, dict(self.unsupported)))
            return ipp.IppResponse(self.print_status, groups)
        if op == ipp.OP_GET_JOB_ATTRIBUTES:
            return ipp.IppResponse(0, [(ipp.TAG_JOB_ATTRS, {"job-id": 641, "job-state": 5})])
        if op in (ipp.OP_VALIDATE_JOB, ipp.OP_CANCEL_JOB):
            return ipp.IppResponse(0, [])
        raise AssertionError(f"unexpected IPP op {op:#x}")


@pytest.fixture
def printer(monkeypatch, tmp_path):
    """Fake printer + fake raster conversion (3-page PDF unless changed)."""
    fake = FakePrinter()
    state = {"pages": 3}

    def fake_pdf_to_pwg(pdf, printer_uri, sides, color, quality, media, workdir):
        out = workdir / "job.pwg"
        out.write_bytes(_pwg(state["pages"]))
        return out

    monkeypatch.setattr(ipp, "ipp_request", fake)
    monkeypatch.setattr(ipp, "pdf_to_pwg", fake_pdf_to_pwg)
    fake.sleeps = []
    monkeypatch.setattr(ipp.time, "sleep", fake.sleeps.append)
    monkeypatch.setattr(ipp, "stdout_is_stderr", lambda: False)
    monkeypatch.setenv("NIXCLAW_CHANNEL_ID", CRC)
    monkeypatch.delenv("NIXCLAW_CONSENT", raising=False)
    doc = tmp_path / "herbarium.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    fake.doc = doc
    fake.state = state
    fake.tmp = tmp_path
    return fake


def _print(printer, monkeypatch, *extra, file=None):
    argv = ["ipp_print.py", "print", URI, str(file or printer.doc),
            "--tmpdir", str(printer.tmp / "work"), *extra]
    monkeypatch.setattr(ipp.sys, "argv", argv)
    return ipp.main()


def _markers(err: str):
    return [line for line in err.splitlines() if line.startswith("NIXCLAW_")]


def test_first_run_emits_marker_and_info_without_any_job(printer, monkeypatch, capsys):
    code = _print(printer, monkeypatch, "--copies=3")
    out, err = capsys.readouterr()
    assert code == 0
    assert _markers(err) == [
        f'NIXCLAW_REQUIRE_CONSENT_QUESTION_{CRC}='
        f'"herbarium.pdf print: 3 copies, 9 sheets, one-sided, b/w, normal?"'
    ]
    assert err.strip() == _markers(err)[0]               # nothing else on stderr
    assert "3 pages × 3 copies = 9 sheets" in out
    assert "NIXCLAW" not in out
    assert ipp.OP_PRINT_JOB not in printer.ops()
    assert printer.ops() == [ipp.OP_GET_PRINTER_ATTRIBUTES]


def test_variable_printer_state_goes_to_stdout_not_into_question(printer, monkeypatch, capsys):
    printer.caps.update({"marker-names": ["Black"], "marker-levels": [4],
                         "marker-low-levels": [10], "queued-job-count": 2})
    _print(printer, monkeypatch)
    out, err = capsys.readouterr()
    assert "ink nearly empty" in out and "Black" in out and "2 job(s)" in out
    assert _markers(err) == [
        f'NIXCLAW_REQUIRE_CONSENT_QUESTION_{CRC}='
        f'"herbarium.pdf print: 3 sheets, one-sided, b/w, normal?"'
    ]


def test_question_is_deterministic(printer, monkeypatch, capsys):
    _print(printer, monkeypatch, "--duplex", "--copies=2")
    first = _markers(capsys.readouterr().err)
    printer.caps["marker-names"], printer.caps["marker-levels"] = ["Cyan"], [1]
    _print(printer, monkeypatch, "--duplex", "--copies=2")
    second = _markers(capsys.readouterr().err)
    assert first == second
    assert first[0].endswith('"herbarium.pdf print: 2 copies, 4 sheets, two-sided, b/w, normal?"')


@pytest.mark.parametrize("bad", [None, "", "1234567", "123456789", "zzzzzzzz"])
def test_missing_or_invalid_channel_id_refuses(bad, printer, monkeypatch, capsys):
    if bad is None:
        monkeypatch.delenv("NIXCLAW_CHANNEL_ID")
    else:
        monkeypatch.setenv("NIXCLAW_CHANNEL_ID", bad)
    code = _print(printer, monkeypatch)
    out, err = capsys.readouterr()
    assert code == 2
    assert "agent's bash tool" in out
    assert _markers(err) == [] and printer.requests == []


def test_shared_stdout_stderr_refused_in_real_process(tmp_path):
    """`2>&1` -> exit 3, hint, no marker, and no printer contact at all."""
    doc = tmp_path / "x.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    env = dict(os.environ, NIXCLAW_CHANNEL_ID=CRC)
    env.pop("NIXCLAW_CONSENT", None)
    proc = subprocess.run(
        [sys.executable, str(MODULE_PATH), "print", URI, str(doc)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=30,
    )
    text = proc.stdout.decode()
    assert proc.returncode == 3
    assert "2>&1" in text and "NIXCLAW_" not in text


def test_separate_pipes_are_not_refused():
    proc = subprocess.run(
        [sys.executable, "-c",
         f"import importlib.util as u; s=u.spec_from_file_location('m', {str(MODULE_PATH)!r}); "
         "m=u.module_from_spec(s); import sys; sys.modules['m']=m; s.loader.exec_module(m); "
         "print(m.stdout_is_stderr())"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    assert proc.stdout.decode().strip() == "False"


def test_consent_run_repeats_question_submits_once_then_ok(printer, monkeypatch, capsys):
    _print(printer, monkeypatch, "--copies=3")
    approved = _markers(capsys.readouterr().err)
    printer.requests.clear()
    monkeypatch.setenv("NIXCLAW_CONSENT", "1")
    code = _print(printer, monkeypatch, "--copies=3")
    out, err = capsys.readouterr()
    assert code == 0
    assert _markers(err) == approved + [f"NIXCLAW_CONSENT_OK_{CRC}"]
    assert printer.ops().count(ipp.OP_PRINT_JOB) == 1
    # default: no waiting loop, at most one status check
    assert printer.ops().count(ipp.OP_GET_JOB_ATTRIBUTES) <= 1
    assert "641" in out


def test_consent_marker_comes_before_the_print_job(printer, monkeypatch, capsys):
    monkeypatch.setenv("NIXCLAW_CONSENT", "1")
    order = []
    real_emit = ipp.emit_marker
    monkeypatch.setattr(ipp, "emit_marker", lambda line: (order.append(line), real_emit(line)))
    inner = ipp.ipp_request

    def spy(uri, payload, timeout=60):
        order.append(struct.unpack(">H", payload[2:4])[0])
        return inner(uri, payload, timeout)

    monkeypatch.setattr(ipp, "ipp_request", spy)
    monkeypatch.setattr(ipp.time, "sleep", lambda secs: order.append(("sleep", secs)))
    assert _print(printer, monkeypatch) == 0
    pj = order.index(ipp.OP_PRINT_JOB)
    # marker, then a pause (harness may kill on a wrong marker), then the job
    assert str(order[pj - 2]).startswith("NIXCLAW_REQUIRE_CONSENT_QUESTION_")
    assert order[pj - 1] == ("sleep", ipp.CONSENT_SUBMIT_DELAY_SECONDS)
    assert ipp.CONSENT_SUBMIT_DELAY_SECONDS >= 1
    assert str(order[-1]).startswith("NIXCLAW_CONSENT_OK_")


def test_first_run_removes_temp_dir_before_marker(printer, monkeypatch, capsys):
    work = printer.tmp / "work"
    seen = []
    real_emit = ipp.emit_marker
    monkeypatch.setattr(ipp, "emit_marker",
                        lambda line: (seen.append(sorted(work.iterdir())), real_emit(line)))
    assert _print(printer, monkeypatch, "--copies=2") == 0
    assert seen == [[]]                                   # nothing left at marker time
    assert printer.sleeps == []                           # no pause on the first run


@pytest.mark.parametrize("kind", ["rejected", "substituted", "transport"])
def test_failed_submit_has_no_ok_marker(kind, printer, monkeypatch, capsys):
    if kind == "rejected":
        printer.print_status = 0x040B
        printer.unsupported = {"sides": "two-sided-long-edge"}
    elif kind == "substituted":                        # printer ignored fidelity
        printer.print_status = 0x0001
        printer.unsupported = {"print-color-mode": "monochrome"}
    else:
        printer.fail_print = True
    monkeypatch.setenv("NIXCLAW_CONSENT", "1")
    code = _print(printer, monkeypatch)
    out, err = capsys.readouterr()
    assert code == 1
    assert not any(m.startswith("NIXCLAW_CONSENT_OK_") for m in _markers(err))
    assert printer.ops().count(ipp.OP_PRINT_JOB) == 1   # never retried
    if kind == "substituted":
        assert ipp.OP_CANCEL_JOB in printer.ops()       # wrong-settings job canceled
        assert "may already have been printed" in out
    if kind == "transport":
        assert "`jobs`" in out


def test_validation_error_on_first_run_has_no_marker(printer, monkeypatch, capsys):
    code = _print(printer, monkeypatch, "--media=na_letter_8.5x11in")
    out, err = capsys.readouterr()
    assert code == 1 and "Error" in out
    assert _markers(err) == [] and ipp.OP_PRINT_JOB not in printer.ops()


def test_print_job_carries_fidelity_boolean_in_operation_group(printer, monkeypatch, capsys):
    monkeypatch.setenv("NIXCLAW_CONSENT", "1")
    _print(printer, monkeypatch)
    (payload,) = printer.payloads(ipp.OP_PRINT_JOB)
    assert FIDELITY == (bytes([ipp.TAG_BOOLEAN]) + struct.pack(">H", 22)
                        + b"ipp-attribute-fidelity" + b"\x00\x01\x01")
    assert 0 < payload.index(FIDELITY) < payload.index(bytes([ipp.TAG_JOB_ATTRS]) + ipp._encode_attr("sides", "one-sided"))


def test_raster_copies_are_made_client_side(printer, monkeypatch, capsys):
    monkeypatch.setenv("NIXCLAW_CONSENT", "1")
    assert _print(printer, monkeypatch, "--copies=3") == 0
    (payload,) = printer.payloads(ipp.OP_PRINT_JOB)
    data = payload[payload.index(ipp.PWG_SYNC):]
    assert len(ipp.pwg_split_pages(data)) == 9
    assert data == ipp.PWG_SYNC + _pwg(3)[4:] * 3
    assert not _has_copies_attr(payload)                  # never sent (fidelity!)


def test_duplex_odd_pages_get_blank_back_page_per_copy(printer, monkeypatch, capsys):
    monkeypatch.setenv("NIXCLAW_CONSENT", "1")
    assert _print(printer, monkeypatch, "--copies=2", "--duplex") == 0
    (payload,) = printer.payloads(ipp.OP_PRINT_JOB)
    pages = ipp.pwg_split_pages(payload[payload.index(ipp.PWG_SYNC):])
    assert len(pages) == 8                                # (3 + blank) x 2
    assert pages[4][1] == pages[0][1]                     # copy 2 starts on a new sheet
    assert set(pages[3][1][2:3]) == {0xFF}                # white pixel (sGray)


def test_single_jpeg_copy_goes_direct_without_copies_attr(printer, monkeypatch, capsys, tmp_path):
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(b"\xff\xd8\xff fake jpeg")
    monkeypatch.setenv("NIXCLAW_CONSENT", "1")
    assert _print(printer, monkeypatch, file=photo) == 0
    (payload,) = printer.payloads(ipp.OP_PRINT_JOB)
    assert ipp._encode_attr("document-format", "image/jpeg") in payload
    assert payload.endswith(photo.read_bytes())
    assert not _has_copies_attr(payload)


def test_jpeg_copies_always_switch_to_raster(printer, monkeypatch, capsys, tmp_path):
    """Even with copies-supported=1-99 the copies are made client-side."""
    printer.state["pages"] = 1
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(b"\xff\xd8\xff fake jpeg")
    monkeypatch.setattr(ipp, "image_to_pdf", lambda src, workdir: src)
    monkeypatch.setenv("NIXCLAW_CONSENT", "1")
    assert _print(printer, monkeypatch, "--copies=4", file=photo) == 0
    (payload,) = printer.payloads(ipp.OP_PRINT_JOB)
    assert ipp._encode_attr("document-format", "image/pwg-raster") in payload
    assert len(ipp.pwg_split_pages(payload[payload.index(ipp.PWG_SYNC):])) == 4
    assert not _has_copies_attr(payload)


def test_explicit_jpeg_format_with_copies_is_an_error(printer, monkeypatch, capsys, tmp_path):
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(b"\xff\xd8\xff fake jpeg")
    assert _print(printer, monkeypatch, "--copies=2", "--format=jpeg", file=photo) == 1
    assert _markers(capsys.readouterr().err) == []


class _Body:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def _real_transport(printer, monkeypatch, broken_op, how):
    """Use the REAL ipp_request over a fake urlopen; `broken_op` misbehaves."""
    real_parse = ipp.parse_response

    def fake_urlopen(req, **kwargs):
        op = struct.unpack(">H", req.data[2:4])[0]
        if op == broken_op:
            if how == "garbled":
                return _Body(b"\x01\x01")                 # too short -> ValueError
            raise http.client.RemoteDisconnected("printer hung up")
        return _Body(b"FAKE" + req.data)

    def fake_parse(body):
        if body.startswith(b"FAKE"):
            return printer(URI, body[4:])
        return real_parse(body)

    monkeypatch.setattr(ipp, "ipp_request", REAL_IPP_REQUEST)
    monkeypatch.setattr(ipp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ipp, "parse_response", fake_parse)


@pytest.mark.parametrize("how", ["garbled", "http-exception"])
def test_unreadable_print_job_reply_says_unknown(how, printer, monkeypatch, capsys):
    _real_transport(printer, monkeypatch, ipp.OP_PRINT_JOB, how)
    monkeypatch.setenv("NIXCLAW_CONSENT", "1")
    code = _print(printer, monkeypatch)
    out, err = capsys.readouterr()
    assert code == 1
    assert "UNKNOWN whether the printer received the job" in out and "`jobs`" in out
    assert not any(m.startswith("NIXCLAW_CONSENT_OK_") for m in _markers(err))


@pytest.mark.parametrize("how", ["garbled", "http-exception"])
def test_unreadable_status_check_keeps_successful_submit(how, printer, monkeypatch, capsys):
    _real_transport(printer, monkeypatch, ipp.OP_GET_JOB_ATTRIBUTES, how)
    monkeypatch.setenv("NIXCLAW_CONSENT", "1")
    code = _print(printer, monkeypatch)
    out, err = capsys.readouterr()
    assert code == 0 and "WAS submitted" in out
    assert _markers(err)[-1] == f"NIXCLAW_CONSENT_OK_{CRC}"


def test_sheet_cap(printer, monkeypatch, capsys):
    printer.state["pages"] = 9
    code = _print(printer, monkeypatch, "--copies=6")     # 54 sheets
    out, err = capsys.readouterr()
    assert code == 1 and "50" in out
    assert _markers(err) == [] and ipp.OP_PRINT_JOB not in printer.ops()
    assert _print(printer, monkeypatch, "--copies=10", "--duplex") == 0   # 5 x 10 = 50 is OK
    assert "50 sheets" in _markers(capsys.readouterr().err)[0]


def test_copies_above_cap_rejected_before_printer_contact(printer, monkeypatch, capsys):
    assert _print(printer, monkeypatch, "--copies=51") == 1
    assert printer.requests == []


@pytest.mark.parametrize("name", ['say "hi".pdf', "two\nlines.pdf", "tab\there\r.pdf"])
def test_question_sanitised(name, printer, monkeypatch, capsys, tmp_path):
    doc = tmp_path / name
    doc.write_bytes(b"%PDF-1.4 fake")
    assert _print(printer, monkeypatch, file=doc) == 0
    (marker,) = _markers(capsys.readouterr().err)
    question = marker.split("=", 1)[1]
    assert question.startswith('"') and question.endswith('"')
    inner = question[1:-1]
    assert '"' not in inner and "\n" not in inner and "\r" not in inner and "\t" not in inner
    assert 0 < len(inner) <= ipp.MAX_QUESTION_CHARS


def test_question_long_filename_truncated(printer, monkeypatch, capsys, tmp_path):
    doc = tmp_path / ("ä" * 30 + "a" * 180 + ".pdf")   # 244 bytes, 214 chars
    doc.write_bytes(b"%PDF-1.4 fake")
    assert _print(printer, monkeypatch, file=doc) == 0
    (marker,) = _markers(capsys.readouterr().err)
    inner = marker.split("=", 1)[1][1:-1]
    assert len(inner) <= 200 and inner.startswith("ä") and "…" in inner and inner.endswith("print: 3 sheets, one-sided, b/w, normal?")


def test_validate_needs_no_consent_env(printer, monkeypatch, capsys):
    monkeypatch.delenv("NIXCLAW_CHANNEL_ID")
    monkeypatch.setattr(ipp.sys, "argv", ["ipp_print.py", "validate", URI, str(printer.doc), "--copies=2",
                                          "--tmpdir", str(printer.tmp / "work")])
    assert ipp.main() == 0
    assert ipp.OP_VALIDATE_JOB in printer.ops() and ipp.OP_PRINT_JOB not in printer.ops()
    assert FIDELITY in printer.payloads(ipp.OP_VALIDATE_JOB)[0]
    assert not _has_copies_attr(printer.payloads(ipp.OP_VALIDATE_JOB)[0])
    assert _markers(capsys.readouterr().err) == []


def test_wait_flag_default_off_and_no_wait_alias(monkeypatch):
    seen = {}
    monkeypatch.setattr(ipp, "cmd_print", lambda args: seen.setdefault("w", []).append(args.wait) or 0)
    for extra in ([], ["--no-wait"], ["--wait"]):
        monkeypatch.setattr(ipp.sys, "argv", ["ipp_print.py", "print", URI, "/tmp/x.pdf", *extra])
        assert ipp.main() == 0
    assert seen["w"] == [False, False, True]


def test_pwg_blank_page_roundtrip_and_total_page_count():
    data = ipp.PWG_SYNC + _pwg_page(1, total_pages=1, color_space=3)
    out = ipp.pwg_repeat_copies(ipp.pwg_split_pages(data), 3, pad_to_even=True)
    pages = ipp.pwg_split_pages(out)
    assert len(pages) == 6
    assert all(struct.unpack(">I", h[452:456])[0] == 6 for h, _ in pages)
    assert set(pages[1][1][2:3]) == {0x00}                # K colour space: 0 = white


def test_pwg_truncated_stream_raises():
    with pytest.raises(ValueError):
        ipp.pwg_split_pages(_pwg(2)[:-3])
