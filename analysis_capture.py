"""Optional lossless trace/provenance capture alongside the existing parser.

No SQL or database imports belong here. Ordinary reports do not use this data.
"""

import hashlib
from pathlib import Path

import patterns as p


def file_signature(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class AnalysisCapture:
    def __init__(self):
        self.source = None
        self.sha256 = None
        self.signature = None
        self.occurrences = {}
        self.errors = []
        self._stack = None
        self.complete = False

    def prepare(self, source):
        if self.source is not None:
            raise ValueError("Use a fresh AnalysisCapture for each analysis.")
        self.source = Path(source).resolve()
        self.signature = file_signature(self.source)
        digest = hashlib.sha256()
        with self.source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        self.sha256 = digest.hexdigest()
        self.verify_source()

    def verify_source(self):
        if self.source is None or file_signature(self.source) != self.signature:
            raise ValueError("Source log changed during analysis/import; retry after the log is closed.")

    def observe(self, item, raw):
        if self._stack is not None and p.STACK_FRAME.search(item["message"]):
            self._stack.append({"line": item["line"], "text": raw})
            return
        self._stack = None
        if not (item["categories"] or item["severity"]):
            return
        self.occurrences.setdefault(item["message"], []).append(
            {"line": item["line"], "timestamp": item["timestamp"]})
        if item["severity"] in {"ERROR", "EXCEPTION", "FATAL"}:
            error = {**item, "full_stack": []}
            self.errors.append(error)
            self._stack = error["full_stack"]
        elif p.SEVERITIES["EXCEPTION"].search(item["message"]):
            # The analyzer classifies warning-level exceptions as warnings.
            # Keep their traces as event provenance rather than promoting severity.
            self._stack = []
            self.occurrences[item["message"]][-1]["warning_stack"] = self._stack

    def finish(self):
        self.verify_source()
        self.complete = True
