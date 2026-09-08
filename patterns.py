"""Case-insensitive message registry. Observed and provisional phrases are explicit."""

import re


def rx(value):
    return re.compile(value, re.IGNORECASE)


TIMESTAMP = rx(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}[T ])?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?")
LEADING_TIMESTAMP = rx(r"^\s*\[?(?:\d{4}-\d{2}-\d{2}[T ])?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?\]?\s*")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FIELDS = rx(r"\b([A-Za-z][A-Za-z0-9_ ]*?)=([^,]*)(?:,|$)")
IDENTITY_KEYS = ("npcid", "id", "identity", "officer", "name", "key", "unityinstanceid")
SEVERITIES = {
    "FATAL": rx(r"\bFATAL\b"),
    "ERROR": rx(r"\bERROR\b"),
    "EXCEPTION": rx(r"\b(?:[\w.]*Exception)\b"),
    "WARNING": rx(r"\bWARN(?:ING)?\b"),
}
TRANSITION = rx(r"\bGOON ACTUAL TRANSITION\s*:\s*(?:Classification\s*=\s*)?(UNSPAWNED_TO_SPAWNED|SPAWNED_TO_UNSPAWNED)\b")
CONTEXT = rx(r"\bTRANSITION (BEFORE|AFTER):\s*(.*)")
SWAT = rx(r"\bSWAT\b")
STATE = rx(r"\bState\s*=\s*(\w+)(?:\s*->\s*(\w+))?")
LIVE = rx(r"\b(?:Live|LiveCount|TrackedCount|TrackedUnits)\s*=\s*(\d+)\b")
# Provisional success vocabulary: no successful deployment existed in the audit log.
SUCCESS = rx(r"\b(?:DEPLOY(?:MENT)? (?:SUCCESS(?:FUL)?|COMPLETE(?:D)?)|DEPLOYED)\b")
NEGATIVE = rx(r"\b(?:not|never|cannot|failed|rejected|blocked|pending|would|expected|unavailable)\b")
TRACKED = rx(r"\b(?:TRACKED UNIT|UNIT TRACKED)\s*:\s*(?:ID|NPCID|Identity)\s*=\s*[^\s,]+")
TEST_VERDICT = rx(r"\b(?:TEST(?:[ -]RUN)?(?: VERDICT| RESULT)?|VERDICT)\s*[:=]\s*(PASS(?:ED)?|FAIL(?:ED)?|BLOCKED|INCONCLUSIVE)\b")
STACK_FRAME = rx(r"^\s*(?:\[[^\]]*\]\s*)*(?:at\s+\S+|--- End of|--->\s*\S+Exception)")
AUDIT_START = rx(r"\bBASELINE READ-ONLY NETWORK AUDIT:\s*")
EXPECTED_GATE = rx(r"\bDEPLOY REJECTED:\s*lifecycle research locked\b")
DIAGNOSTIC_RESULT = rx(r"\b(?:REGISTRY MEMBERSHIP|SCENE REGISTRY|PREFAB (?:CHECK|CATALOG)|POOL CHECK|SOURCE BEFORE|POPULATION READY|TEST VERDICT|STATION ADMISSION RESULT)\b")

# Seeded from saved MelonLoader audit logs and local C# logger emit sites.
# General fallback categories classify observations only; they never prove success.
PATTERNS = {
    "initialization": rx(r"(?:Hyland[ _]?Heat.*\b(?:loaded|initialized|initialization|startup)\b|\bSESSION START\b)"),
    "shutdown": rx(r"(?:Hyland[ _]?Heat.*\b(?:shutdown|unloaded)\b|\bSESSION END\b)"),
    "clone_population": rx(r"\b(?:OFFICER CLONE (?:POPULATION|QUEUE)|INACTIVE CLONE (?:CREATED|TEST))\b"),
    "officer_registry": rx(r"\b(?:Officer\w*|REGISTRY MEMBERSHIP|SCENE REGISTRY|GOON DUPLICATE AUDIT)\b"),
    "identity": rx(r"\b(?:NPCID|BakedGUID|RuntimeGUID|SceneId|ObjectId|identity)\b"),
    "duplicate_identity": rx(r"\bDUPLICATE[ _-]IDENTIT(?:Y|IES)\b(?!\s*[:=]\s*(?:none|false|0)\b)"),
    "missing": rx(r"\b(?:GOON|OFFICER|CLONE|INSTANCE|IDENTITY)[ _].*?\bMISSING\b"),
    "replacement": rx(r"\b(?:INSTANCE_REPLACED|REPLACEMENT|REPLACED)\b"),
    "appearance": rx(r"\b(?:INSTANCE_APPEARED|APPEARED|APPEARANCE)\b"),
    "disappearance": rx(r"\b(?:INSTANCE_DISAPPEARED|DISAPPEARED|DISAPPEARANCE)\b"),
    "goon_observation": rx(r"\b(?:GOON|TRANSITION BEFORE|TRANSITION AFTER|IDENTITY PRESERVED|SAME INSTANCE|LOGICAL POOL TRANSITION)\b"),
    "swat": SWAT,
    "swat_deploy": rx(r"\bDEPLOY(?:MENT)? (?:ATTEMPT|REQUEST|REJECTED|SUCCESS(?:FUL)?|COMPLETE(?:D)?)\b"),
    "swat_rejection": rx(r"\bDEPLOY(?:MENT)? (?:REJECTED|FAILED|BLOCKED)\b"),
    "swat_recall": rx(r"\bRECALL (?:IGNORED|REQUESTED|STARTED|COMPLETE(?:D)?|SUCCESS|FAILED)\b"),
    "swat_readiness": rx(r"\b(?:READY|READINESS|DEPLOYMENT LOCKED|PREFAB CATALOG)\b"),
    "swat_tracking": rx(r"\b(?:TRACKED|TRACKING|Live|LiveCount|State)\b"),
    "police": rx(r"\b(?:POLICE|PURSUIT)\b"),
    "infamy": rx(r"\bINFAMY\b"),
    "suspicion": rx(r"\bSUSPICION\b"),
    "crime": rx(r"\bCRIME\w*\b"),
    "patrol": rx(r"\bPATROL\w*\b"),
    "test_event": rx(r"\b(?:TEST[ -]RUN|TEST (?:BEFORE|AFTER|START(?:ED)?|END|RESULT|VERDICT|FAIL(?:ED|URE)?|PASS(?:ED)?|BLOCKED)|(?:GUID|DATA|STATION ADMISSION) TEST:|(?:GOON )?SNAPSHOT:|VERDICT\s*[:=]|PREREQUISITE\s*[:=]|(?:NETWORK|DUPLICATE) AUDIT:)"),
    "diagnostic_result": DIAGNOSTIC_RESULT,
    "read_only_audit": AUDIT_START,
    "test_failure": rx(r"\b(?:TEST(?:[ -]RUN)?|VERDICT|PREREQUISITE)\b.*\b(?:FAIL(?:ED|URE)?|BLOCKED)\b"),
    "integrity_failure": rx(r"\b(?:POOL_MEMBERSHIP_INCONSISTENT|LOGICAL POOL TRANSITION VALID=False|(?:NETWORK|GAME) IDENTITY PRESERVED=False)\b"),
}


def classify(message):
    return [name for name, pattern in PATTERNS.items()
            if pattern.search(message) and (not name.startswith("swat_") or SWAT.search(message))]
