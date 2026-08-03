"""Mock 证据数据 (勒索病毒场景)

对齐 01_phase1_design.md §13 TR-002 对策: 无真实数据源时 mock 降级, 保证流水线可端到端跑通。
供 S3/S4 在无 SIEM/MISP 环境下开发时间线/攻击链/报告。
"""
from __future__ import annotations

from typing import Any

from src.app.traceback.schemas.evidence import CollectContext, RawEvidence


def _first_asset(ctx: CollectContext) -> str:
    return (ctx.asset_ips or ctx.asset_hosts or ["10.0.0.5"])[0]


def mock_siem_logs(ctx: CollectContext) -> list[RawEvidence]:
    asset = _first_asset(ctx)
    logs = [
        {"ts": "2026-07-01T08:05:00Z", "src": "external", "dst": asset,
         "event": "phishing_email_received", "subject": "发票确认"},
        {"ts": "2026-07-01T08:12:00Z", "src": asset, "dst": "203.0.113.55",
         "event": "powershell_execution", "cmd": "powershell -enc <base64>"},
        {"ts": "2026-07-01T08:30:00Z", "src": asset, "dst": "203.0.113.55",
         "event": "c2_beacon", "port": 443, "interval_sec": 60},
        {"ts": "2026-07-01T09:10:00Z", "src": asset, "dst": "10.0.0.6",
         "event": "rdp_lateral", "user": "admin"},
        {"ts": "2026-07-01T09:45:00Z", "src": asset, "event": "file_encryption",
         "extension": ".locked", "count": 1200},
    ]
    return [
        RawEvidence(
            evidence_type="log", source="siem", raw=log,
            parsed={"event": log["event"], "ts": log["ts"]},
            related_assets=[asset], importance="high",
        )
        for log in logs
    ]


def mock_edr_behavior(ctx: CollectContext) -> list[RawEvidence]:
    asset = _first_asset(ctx)
    procs = [
        {"host": asset, "pid": 1234, "name": "powershell.exe",
         "parent": "outlook.exe", "cmd": "powershell -enc ..."},
        {"host": asset, "pid": 2222, "name": "lsass.exe",
         "note": "credential_dump_suspected"},
        {"host": asset, "pid": 3333, "name": "ransomware.exe",
         "note": "mass_file_write"},
    ]
    return [
        RawEvidence(
            evidence_type="log", source="edr", raw=p, parsed=p,
            related_assets=[asset], importance="high",
        )
        for p in procs
    ]


def mock_dns_records(ctx: CollectContext) -> list[RawEvidence]:
    records = [
        {"domain": "cdn-update-check.com", "resolved": "203.0.113.55",
         "type": "A", "first_seen": "2026-07-01T08:00:00Z"},
        {"domain": "malicious-c2.example", "resolved": "203.0.113.55",
         "type": "A", "ttl": 60},
    ]
    return [
        RawEvidence(
            evidence_type="dns", source="dns", raw=r, parsed=r,
            related_assets=[], importance="medium",
        )
        for r in records
    ]


def mock_threat_intel(ctx: CollectContext) -> list[RawEvidence]:
    iocs = [
        {"ioc_type": "ip", "value": "203.0.113.55", "malicious": True,
         "threat_type": "C2", "malware_family": "RansomX", "confidence": 0.92},
        {"ioc_type": "domain", "value": "malicious-c2.example", "malicious": True,
         "threat_type": "C2", "malware_family": "RansomX", "confidence": 0.88},
    ]
    return [
        RawEvidence(
            evidence_type="intel", source="misp", raw=ioc, parsed=ioc,
            related_assets=[], importance="high",
        )
        for ioc in iocs
    ]


_MOCK_DISPATCH: dict[str, Any] = {
    "siem": mock_siem_logs,
    "edr": mock_edr_behavior,
    "dns": mock_dns_records,
    "misp": mock_threat_intel,
}


# ---- P2 深度取证 mock 数据 (§4) ----

def mock_zeek_traffic(ctx: CollectContext) -> list[RawEvidence]:
    asset = _first_asset(ctx)
    return [
        RawEvidence(
            evidence_type="traffic", source="zeek", raw={
                "log_type": "conn", "src": asset, "dst": "203.0.113.55",
                "ts": "2026-07-01T08:30:00Z", "duration": 60, "bytes": 1024,
                "note": "c2_beacon_fixed_interval",
            }, parsed={"note": "c2_beacon"}, related_assets=[asset, "203.0.113.55"],
            importance="high",
        ),
        RawEvidence(
            evidence_type="traffic", source="zeek", raw={
                "log_type": "dns", "query": "asdjkwza.malicious-c2.example",
                "ts": "2026-07-01T08:28:00Z", "note": "dga_suspected",
            }, parsed={"note": "dga"}, related_assets=[], importance="medium",
        ),
    ]


def mock_velociraptor_artifacts(ctx: CollectContext) -> list[RawEvidence]:
    asset = _first_asset(ctx)
    return [
        RawEvidence(
            evidence_type="endpoint_deep", source="velociraptor", raw={
                "artifact": "Windows.System.Pslist", "host": asset,
                "process": "ransomware.exe", "pid": 3333, "parent": "powershell.exe",
            }, parsed={"artifact": "Pslist", "host": asset}, related_assets=[asset],
            importance="high",
        ),
        RawEvidence(
            evidence_type="endpoint_deep", source="velociraptor", raw={
                "artifact": "Windows.Network.Netstat", "host": asset,
                "remote": "203.0.113.55:443", "state": "ESTABLISHED",
            }, parsed={"artifact": "Netstat", "c2": "203.0.113.55"}, related_assets=[asset],
            importance="high",
        ),
    ]


def mock_yara_matches(ctx: CollectContext) -> list[RawEvidence]:
    return [
        RawEvidence(
            evidence_type="sample", source="yara", raw={
                "rule": "RansomX_Loader", "file": "C:/temp/loader.exe",
                "malware_family": "RansomX", "md5": "aabbccdd11223344",
            }, parsed={"malware_family": "RansomX", "rule": "RansomX_Loader"},
            related_assets=[], importance="high",
        ),
    ]


def mock_cape_report(ctx: CollectContext) -> list[RawEvidence]:
    return [
        RawEvidence(
            evidence_type="sample", source="cape", raw={
                "sample": "loader.exe", "family": "RansomX", "score": 8.5,
                "network": [{"ip": "203.0.113.55", "port": 443}],
                "dropped": ["ransomware.dll"], "behavior": ["encrypt_files", "c2_comm"],
            }, parsed={"malware_family": "RansomX", "score": 8.5},
            related_assets=[], importance="high",
        ),
    ]


def mock_volatility_findings(ctx: CollectContext) -> list[RawEvidence]:
    asset = _first_asset(ctx)
    return [
        RawEvidence(
            evidence_type="memory", source="volatility", raw={
                "plugin": "malfind", "host": asset, "process": "lsass.exe", "pid": 2222,
                "note": "injected_code_detected",
            }, parsed={"plugin": "malfind", "note": "injection"}, related_assets=[asset],
            importance="high",
        ),
    ]


_DEEP_MOCK_DISPATCH: dict[str, Any] = {
    "zeek": mock_zeek_traffic,
    "velociraptor": mock_velociraptor_artifacts,
    "yara": mock_yara_matches,
    "cape": mock_cape_report,
    "volatility": mock_volatility_findings,
}


def get_deep_mock_evidence(source: str, ctx: CollectContext) -> list[RawEvidence]:
    fn = _DEEP_MOCK_DISPATCH.get(source)
    return fn(ctx) if fn else []


def get_mock_evidence(source: str, ctx: CollectContext) -> list[RawEvidence]:
    fn = _MOCK_DISPATCH.get(source)
    return fn(ctx) if fn else []
