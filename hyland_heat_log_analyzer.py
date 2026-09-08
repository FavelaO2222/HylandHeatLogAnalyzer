"""Offline, standard-library log evidence analyzer. Run with --help for usage."""

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

import patterns as p

PROFILES = ("general", "read-only-audit", "goon-lifecycle", "swat-deployment")


def evidence_priority(event):
    categories = set(event["categories"])
    if event["severity"] in {"ERROR", "EXCEPTION", "FATAL"} or categories & {
            "duplicate_identity", "test_failure", "integrity_failure"}:
        rank = 0
    elif event["test_verdict"] or categories & {"swat_rejection", "goon_transition"}:
        rank = 1
    elif event["severity"]:
        rank = 2
    elif categories & {"diagnostic_result", "read_only_audit"}:
        rank = 3
    else:
        rank = 4
    return rank, -event["last_line"]


def evaluate(events, counts, severities, transitions, confirmations, metadata, profile):
    """Evaluate only explicitly selected requirements; never infer test intent."""
    reasons, missing, recommendations = [], [], []
    rejected = [e for e in events if "swat_rejection" in e["categories"]]
    expected = []
    if profile == "read-only-audit":
        for event in rejected:
            state, live = p.STATE.search(event["message"]), p.LIVE.search(event["message"])
            if (p.EXPECTED_GATE.search(event["message"]) and state and live
                    and (state.group(2) or state.group(1)).lower() == "dormant"
                    and int(live.group(1)) == 0):
                expected.append(event["line"])
    failures = [name for name in ("duplicate_identity", "test_failure", "integrity_failure") if counts[name]]
    if any(e["line"] not in expected for e in rejected):
        failures.append("unexpected swat_rejection")
    problems = sum(severities[s] for s in ("ERROR", "EXCEPTION", "FATAL"))
    if problems:
        reasons.append(f"{problems} error/exception/fatal lines (may originate in other mods).")
    if failures:
        reasons.append("Detected: " + ", ".join(failures) + ".")
    if profile in {"general", "goon-lifecycle"} and not all(transitions[k] for k in (
            "UNSPAWNED_TO_SPAWNED", "SPAWNED_TO_UNSPAWNED")):
        missing.append("Both goon transition directions have not been observed.")
        recommendations.append("For an authorized goon observation test, capture both actual transition directions and their BEFORE/AFTER snapshots.")
    if profile in {"general", "swat-deployment"} and not confirmations:
        missing.append("SWAT deployment with positive live/tracked evidence is unconfirmed.")
        if not rejected:
            recommendations.append("For an already authorized SWAT test, capture deployment outcome plus live/tracked-unit evidence.")
    if profile == "read-only-audit":
        if not counts["read_only_audit"]:
            missing.append("No explicit read-only network audit baseline was logged.")
        for marker in ("REGISTRY MEMBERSHIP", "SCENE REGISTRY"):
            if not any(marker in e["message"].upper() for e in events):
                missing.append(f"No {marker} evidence was logged.")
        dormant = False
        unexpected_activity = []
        for event in events:
            if "swat" not in event["categories"]:
                continue
            state, live = p.STATE.search(event["message"]), p.LIVE.search(event["message"])
            final = (state.group(2) or state.group(1)).lower() if state else None
            dormant |= final == "dormant" and live is not None and int(live.group(1)) == 0
            if (final is not None and final != "dormant") or (live and int(live.group(1)) > 0) or p.TRACKED.search(event["message"]):
                unexpected_activity.append(event["line"])
        if not dormant:
            missing.append("No State=Dormant, Live=0 observation was logged.")
        if unexpected_activity or confirmations:
            failures.append("activity during a read-only audit")
            reasons.append(f"SWAT activity conflicts with read-only scope; source lines: {unexpected_activity[:10]}.")
        if expected:
            reasons.append(f"Expected lifecycle gate rejections observed at lines {expected}; deployment is not required for this profile.")
        recommendations.append("Review captured registry/identity findings before choosing further work; deployment and goon transitions are outside this audit's requirements.")
    if any(e["test_verdict"] == "INCONCLUSIVE" for e in events):
        missing.append("An explicit test verdict is INCONCLUSIVE.")
    warning_count = sum(e["count"] for e in events if e["severity"] == "WARNING" and e["line"] not in expected)
    if warning_count or metadata["decode_replacement_lines"]:
        missing.append("Warnings or replacement characters require review before an evidence PASS.")
    verdict = "NEEDS ATTENTION" if problems or failures else "INCONCLUSIVE" if missing else "PASS"
    if verdict == "PASS":
        reasons.append(f"Required logged observations for {profile} are present; this does not prove gameplay, safe identity ownership, or teardown.")
    if problems or failures:
        recommendations.insert(0, "Review cited errors and failures before another playtest.")
    if rejected:
        recommendations.append("Keep deployment gates in place; this report does not authorize spawning.")
    if not recommendations:
        recommendations.append("Review identity ownership and teardown separately before expanding test scope.")
    return {"verdict": verdict, "verdict_reasons": reasons + missing,
            "missing_evidence": missing, "recommendations": recommendations,
            "expected_rejection_lines": expected, "profile": profile}

def fields(message):
    return {key.strip().lower(): value.strip() for key, value in p.FIELDS.findall(message)}


def parse_line(raw, line_number):
    clean = p.ANSI.sub("", raw.rstrip("\r\n"))
    stamp = p.TIMESTAMP.search(clean)
    message = p.LEADING_TIMESTAMP.sub("", clean).strip()
    severity = next((name for name, regex in p.SEVERITIES.items() if regex.search(message)), None)
    categories = p.classify(message)
    match = p.TRANSITION.search(message)
    if match:
        categories.append("goon_transition")
    verdict = p.TEST_VERDICT.search(message)
    data = fields(message)
    return {
        "line": line_number, "timestamp": stamp.group() if stamp else None,
        "message": message, "raw": raw.rstrip("\r\n"), "severity": severity,
        "categories": categories, "fields": data,
        "identity": next((data[k] for k in p.IDENTITY_KEYS if data.get(k)), None),
        "transition": match.group(1).upper() if match else None,
        "test_verdict": verdict.group(1).upper() if verdict else None,
    }


def analyze(source, profile="general", *, capture=None):
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile: {profile}")
    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in {".log", ".txt"}:
        raise ValueError("Input must be a regular .log or .txt file.")
    if capture is not None:
        capture.prepare(source)
    metadata = {"source_file": source.name, "source_path": str(source),
                "size_bytes": source.stat().st_size, "lines_scanned": 0,
                "first_timestamp": None, "last_timestamp": None, "decode_replacement_lines": 0}
    events, transitions, identities = {}, [], {}
    severity_counts, category_counts = Counter(), Counter()
    matching_lines = 0
    pending = None
    deployment_state = None
    confirmations = []
    stack_owner = None
    # Unique messages are retained; repeated lines only increment counters.
    with source.open("r", encoding="utf-8-sig", errors="replace", newline="" if capture is not None else None) as handle:
        for number, raw in enumerate(handle, 1):
            metadata["lines_scanned"] = number
            metadata["decode_replacement_lines"] += int("\ufffd" in raw)
            item = parse_line(raw, number)
            if capture is not None:
                capture.observe(item, raw)
            if item["timestamp"]:
                metadata["first_timestamp"] = metadata["first_timestamp"] or item["timestamp"]
                metadata["last_timestamp"] = item["timestamp"]
            if stack_owner is not None and p.STACK_FRAME.search(item["message"]):
                if stack_owner["count"] > 1:
                    continue
                if len(stack_owner["stack_trace"]) < 40:
                    stack_owner["stack_trace"].append({"line": number, "raw": item["raw"]})
                else:
                    stack_owner["stack_trace_omitted"] += 1
                continue
            stack_owner = None
            categories = item["categories"]
            if item["severity"]:
                severity_counts[item["severity"]] += 1
            if categories or item["severity"]:
                matching_lines += 1
                category_counts.update(categories)
                key = item["message"]
                if key not in events:
                    events[key] = {**item, "count": 0, "last_line": number,
                                   "stack_trace": [], "stack_trace_omitted": 0}
                events[key]["count"] += 1
                events[key]["last_line"] = number
                if item["severity"] in {"ERROR", "EXCEPTION", "FATAL"} or p.SEVERITIES["EXCEPTION"].search(item["message"]):
                    stack_owner = events[key]
            if item["identity"] and categories:
                identity = item["identity"]
                if identity not in identities:
                    identities[identity] = {"identity": identity, "first_line": number,
                                            "timestamp": item["timestamp"], "observations": 0}
                identities[identity]["observations"] += 1
            if item["transition"]:
                before, after = item["transition"].split("_TO_")
                pending = {"line": number, "timestamp": item["timestamp"],
                           "classification": item["transition"], "identity": item["identity"],
                           "before_state": before, "after_state": after,
                           "before_fields": {}, "after_fields": {}, "source_lines": [number]}
                transitions.append(pending)
            elif pending:
                context = p.CONTEXT.search(item["message"])
                if context and number - pending["line"] <= 2:
                    side = context.group(1).lower()
                    pending[side + "_fields"] = fields(context.group(2))
                    pending["source_lines"].append(number)
                    pending["identity"] = pending["identity"] or item["identity"]
                else:
                    pending = None
            if "initialization" in categories or "shutdown" in categories:
                deployment_state = None
            if "swat" in categories:
                state = p.STATE.search(item["message"])
                live = p.LIVE.search(item["message"])
                positive = bool(live and int(live.group(1)) > 0) or bool(p.TRACKED.search(item["message"]))
                negative = bool(p.NEGATIVE.search(item["message"]))
                final_state = (state.group(2) or state.group(1)).lower() if state else None
                reset = ("swat_rejection" in categories or "swat_recall" in categories
                         or negative or (final_state is not None and final_state != "deployed")
                         or (live is not None and int(live.group(1)) == 0))
                if reset:
                    deployment_state = None
                else:
                    if final_state == "deployed" or p.SUCCESS.search(item["message"]):
                        deployment_state = number
                    # Adjacent observations only: avoid correlating unrelated test sessions.
                    if positive and deployment_state is not None and number - deployment_state <= 5:
                        confirmations.append({"deployment_line": deployment_state, "unit_evidence_line": number})
                        deployment_state = None
    if capture is not None:
        capture.finish()
    event_list = list(events.values())
    repeated = sorted((e for e in event_list if e["count"] > 1), key=lambda e: (-e["count"], e["line"]))
    transition_counts = Counter(t["classification"] for t in transitions)
    evaluation = evaluate(event_list, category_counts, severity_counts, transition_counts,
                          confirmations, metadata, profile)
    important = sorted(event_list, key=evidence_priority)[:30]
    return {"schema_version": 2, "metadata": metadata, **evaluation, "matching_lines": matching_lines,
            "unique_logical_events": len(event_list), "repeat_occurrences": matching_lines - len(event_list),
            "severity_counts": {s: severity_counts[s] for s in p.SEVERITIES},
            "category_counts": dict(category_counts), "detected_events": event_list,
            "transitions": transitions, "transition_counts": dict(transition_counts),
            "officers_identities": list(identities.values()),
            "swat_events": [e for e in event_list if "swat" in e["categories"]],
            "swat_deployment_confirmations": confirmations,
            "test_verdicts": [e for e in event_list if e["test_verdict"]],
            "repeated_messages": repeated, "important_source_lines": important}


def render_report(report):
    m = report["metadata"]
    lines = ["HYLAND HEAT LOG SUMMARY", "=======================", "",
             f"Source file: {m['source_file']} ({m['size_bytes']} bytes)",
             f"Time range: {m['first_timestamp'] or 'unknown'} -> {m['last_timestamp'] or 'unknown'}",
             f"Lines scanned: {m['lines_scanned']}",
             f"Test profile: {report['profile']} (explicit selection; general is the default)",
             f"Matching lines: {report['matching_lines']}; unique logical messages: {report['unique_logical_events']}; repeated occurrences: {report['repeat_occurrences']}",
             f"Lines containing decoding replacement characters: {m['decode_replacement_lines']}",
             "", "OVERALL VERDICT", report["verdict"]]
    lines.extend("- " + reason for reason in report["verdict_reasons"])
    lines.append("Observed facts below; recommendations are listed separately.")

    def section(title, selected, rank=True):
        lines.extend(["", title])
        if rank:
            selected = sorted(selected, key=evidence_priority)
        if not selected:
            lines.append("- No matching evidence.")
        for event in selected[:12]:
            message = event["message"]
            lines.append(f"- {event['count']}x [line {event['line']}, {event['timestamp'] or 'no timestamp'}] {message[:500]}" + (" ... [truncated; see JSON/source]" if len(message) > 500 else ""))
            for frame in event["stack_trace"]:
                lines.append(f"  line {frame['line']}: {frame['raw'][:500]}")
            if event["stack_trace_omitted"]:
                lines.append(f"  {event['stack_trace_omitted']} stack lines omitted; see source.")
        if len(selected) > 12:
            lines.append(f"- {len(selected) - 12} more unique messages omitted; see JSON/source.")

    def select(*categories):
        return [e for e in report["detected_events"] if set(categories).intersection(e["categories"])]

    lines.extend(["", "ERRORS AND WARNINGS", ", ".join(f"{s}={n}" for s, n in report["severity_counts"].items())])
    section("Unique severity messages", [e for e in report["detected_events"] if e["severity"]])
    section("INITIALIZATION / SHUTDOWN", select("initialization", "shutdown"))
    lines.extend(["", "GOON LIFECYCLE TRANSITIONS", f"Counts: {report['transition_counts'] or 'none'}"])
    for t in report["transitions"]:
        lines.append(f"- line {t['line']} [{t['timestamp'] or 'no timestamp'}] {t['classification']}: identity={t['identity'] or 'unknown'}, {t['before_state']} -> {t['after_state']}; source lines={t['source_lines']}")
    section("OFFICER / CLONE REGISTRY", select("officer_registry", "clone_population", "duplicate_identity", "missing", "replacement", "appearance", "disappearance"))
    lines.append("Observed identity labels (not verified distinct officers): " + ", ".join(i["identity"] for i in report["officers_identities"][:30]))
    section("SWAT", report["swat_events"])
    lines.append(f"Confirmed deployment observations: {len(report['swat_deployment_confirmations'])}")
    section("POLICE, INFAMY, SUSPICION, AND PATROLS", select("police", "infamy", "suspicion", "crime", "patrol"))
    section("TEST EVIDENCE", select("test_event", "test_failure", "integrity_failure", "diagnostic_result", "read_only_audit"))
    lines.extend(["", "CATEGORY COUNTS (overlapping matching lines)"])
    lines.extend(f"- {name}: {count}" for name, count in sorted(report["category_counts"].items()))
    lines.extend(["", "RECOMMENDATIONS (not observed facts)"])
    lines.extend("- " + item for item in report["recommendations"])
    section("REPEATED NOISE", report["repeated_messages"], rank=False)
    lines.extend(["", "IMPORTANT SOURCE LINES (first occurrence of selected messages)"])
    for event in report["important_source_lines"]:
        lines.append(f"{event['line']}: {event['raw']}")
    return "\n".join(lines) + "\n"


def render_brief(report, max_chars=6000):
    """Bound the AI entry point; full evidence stays in JSON and the source."""
    m = report["metadata"]
    lines = ["HYLAND HEAT AI BRIEF", f"Source: {m['source_file']}",
             f"Profile: {report['profile']}; verdict: {report['verdict']}",
             f"Scanned: {m['lines_scanned']} lines; unique messages: {report['unique_logical_events']}",
             f"Severity counts: {report['severity_counts']}",
             f"Transition counts: {report['transition_counts']}",
             f"SWAT deployment confirmations: {len(report['swat_deployment_confirmations'])}",
             "ASSESSMENT"]
    lines.extend(report["verdict_reasons"])
    lines.append("RECOMMENDATIONS (not observations)")
    lines.extend(report["recommendations"])
    lines.append("SELECTED EVIDENCE (original source line numbers)")
    footer = "\nSelection is incomplete. Use the detailed summary, JSON, or cited source lines for follow-up.\n"
    text = "\n".join(lines) + "\n"
    for event in sorted(report["detected_events"], key=evidence_priority):
        excerpt = event["message"][:550] + (" ... [truncated]" if len(event["message"]) > 550 else "")
        entry = f"L{event['line']} ({event['count']}x): {excerpt}\n"
        frames = event["stack_trace"]
        # Include both the first calls and the caller near the end of the stack.
        for frame in frames if len(frames) <= 5 else frames[:2] + frames[-3:]:
            entry += f"L{frame['line']}: {frame['raw'][:300]}\n"
        if len(frames) > 5 or event["stack_trace_omitted"]:
            entry += "[Selected stack frames; see detailed summary/source for remaining calls.]\n"
        if len(text) + len(entry) + len(footer) > max_chars:
            continue
        text += entry
    return text[:max_chars - len(footer)] + footer


def write_reports(report, output, include_json=True, include_csv=False, include_brief=False, protected_paths=()):
    source = Path(report["metadata"]["source_path"])
    output = Path(output).expanduser().resolve()
    suffixes = [".txt"] + ([".json"] if include_json else []) + ([".csv"] if include_csv else [])
    paths = [output / (source.stem + "_summary" + suffix) for suffix in suffixes]
    brief_path = output / (source.stem + "_brief.txt")
    if include_brief:
        paths.append(brief_path)
    for path in paths:
        if path.resolve() == source or (path.exists() and path.samefile(source)):
            raise ValueError("Refusing to overwrite the input log through an output alias.")
        for protected in protected_paths:
            if path.resolve() == protected.resolve() or (path.exists() and protected.exists() and path.samefile(protected)):
                raise ValueError("Report destination conflicts with the database path.")
    output.mkdir(parents=True, exist_ok=True)
    for path in paths:
        with path.open("w", encoding="utf-8", newline="") as handle:
            if path == brief_path:
                handle.write(render_brief(report))
            elif path.suffix == ".txt":
                handle.write(render_report(report))
            elif path.suffix == ".json":
                json.dump(report, handle, indent=2, ensure_ascii=True)
                handle.write("\n")
            else:
                writer = csv.writer(handle)
                writer.writerow(["first_line", "last_line", "timestamp", "count", "severity", "categories", "message"])
                for event in report["detected_events"]:
                    writer.writerow([event["line"], event["last_line"], event["timestamp"], event["count"],
                                     event["severity"], "|".join(event["categories"]), event["message"]])
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_file", type=Path)
    parser.add_argument("--output", type=Path, help="Report directory (default: reports beside input)")
    parser.add_argument("--no-json", action="store_true", help="Write only text (and CSV if requested)")
    parser.add_argument("--csv", action="store_true", help="Also export deduplicated events as CSV")
    parser.add_argument("--profile", choices=PROFILES, default="general", help="Explicit test requirements")
    parser.add_argument("--brief", action="store_true", help="Also write an AI brief capped at 6000 characters")
    parser.add_argument("--database", help="Optionally import evidence into a project-relative or absolute SQLite database")
    args = parser.parse_args(argv)
    try:
        capture, database_path, imported = None, None, None
        if args.database is not None:
            from analysis_capture import AnalysisCapture
            from database.ingestion import persist_analysis, validate_destination, format_import_summary
            capture = AnalysisCapture()
            database_path = validate_destination(args.database, args.log_file)
        report = analyze(args.log_file, args.profile, capture=capture)
        output = args.output if args.output is not None else Path(report["metadata"]["source_path"]).parent / "reports"
        paths = write_reports(report, output, not args.no_json, args.csv, args.brief,
                              protected_paths=(database_path,) if database_path is not None else ())
        if database_path is not None:
            imported = persist_analysis(report, capture, database_path)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"{report['verdict']}: scanned {report['metadata']['lines_scanned']} lines; {report['matching_lines']} matching lines.")
    for reason in report["verdict_reasons"]:
        print(reason)
    for path in paths:
        print(f"Report: {path}")
    if imported is not None:
        print(format_import_summary(imported))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
