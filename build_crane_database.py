import hashlib
import re
import sqlite3
import zlib
from pathlib import Path

DB_PATH = Path("cranes.db")

CONFIG_CODE_RE = re.compile(r"\b([A-Z]{1,4}(?:-[A-Z]{1,3})?)\b")
NUMBER_M_RE = re.compile(r"(\d{1,3}(?:[\.,]\d+)?)\s*m\b", re.I)
NUMBER_T_RE = re.compile(r"(\d{1,4}(?:[\.,]\d+)?)\s*t\b", re.I)
TRIPLE_RE = re.compile(
    r"(\d{1,3}(?:[\.,]\d+)?)\s*m\s+"
    r"(\d{1,3}(?:[\.,]\d+)?)\s*m\s+"
    r"(\d{1,4}(?:[\.,]\d+)?)\s*t\b",
    re.I,
)

CONFIG_HINTS = (
    "main boom",
    "hauptausleger",
    "jib",
    "klappspitze",
    "luffing",
    "wipp",
    "fly",
    "swing-away",
    "offset",
    "boom",
)


def parse_filename(pdf_name: str):
    stem = pdf_name.removesuffix(".pdf").replace("_Load_Charts", "")
    stem = re.sub(r"\s+", " ", stem.replace("_", " ")).strip()
    tokens = stem.split()
    manufacturer = tokens[0] if tokens else None
    model = " ".join(tokens[1:]) if len(tokens) > 1 else stem
    return manufacturer, model


def estimate_page_count(pdf_bytes: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b", pdf_bytes))


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_pdf_literal(raw: bytes) -> str:
    raw = (
        raw.replace(b"\\(", b"(")
        .replace(b"\\)", b")")
        .replace(b"\\n", b" ")
        .replace(b"\\r", b" ")
        .replace(b"\\t", b" ")
    )
    if b"\x00" in raw:
        try:
            return raw.decode("utf-16-be", "ignore")
        except Exception:
            pass
    return raw.decode("latin1", "ignore")


def extract_pdf_text(path: Path) -> str:
    data = path.read_bytes()
    chunks: list[str] = []

    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue

        stream = data[start:end]
        if stream.endswith(b"\r\n"):
            stream = stream[:-2]
        elif stream.endswith(b"\n"):
            stream = stream[:-1]

        try:
            decoded = zlib.decompress(stream)
        except Exception:
            continue

        if b"Tj" not in decoded and b"TJ" not in decoded:
            continue

        for literal in re.findall(rb"\((.*?)\)\s*Tj", decoded, re.S):
            text = _decode_pdf_literal(literal).strip()
            if text:
                chunks.append(text)

        for arr in re.findall(rb"\[(.*?)\]\s*TJ", decoded, re.S):
            for literal in re.findall(rb"\((.*?)\)", arr, re.S):
                text = _decode_pdf_literal(literal).strip()
                if text:
                    chunks.append(text)

    text = " ".join(chunks)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_candidates(text: str):
    # Keep extraction permissive: split in medium chunks so config and numbers remain close.
    return re.split(r"\s{2,}|\s\|\s|\s-\s", text)


def extract_configurations(text: str):
    configs: dict[str, str] = {}
    for part in split_candidates(text):
        part_norm = re.sub(r"\s+", " ", part).strip()
        if len(part_norm) < 8:
            continue

        lower = part_norm.lower()
        if not any(h in lower for h in CONFIG_HINTS):
            continue

        codes = CONFIG_CODE_RE.findall(part_norm)
        if not codes:
            continue

        # Use first reasonable code-like token as configuration key.
        code = None
        for c in codes:
            if c in {"PDF", "LOAD", "CHART", "TON", "MT", "DEMAG", "TEREX", "TADANO", "GROVE", "LIEBHERR"}:
                continue
            code = c
            break

        if not code:
            continue

        if code not in configs:
            configs[code] = part_norm[:220]

    return configs


def parse_float(value: str):
    try:
        return float(value.replace(",", "."))
    except Exception:
        return None


def likely_value(v: float, low: float, high: float) -> bool:
    return low <= v <= high


def extract_load_points(text: str, default_config: str = "UNKNOWN"):
    points = []
    current_config = default_config

    for part in split_candidates(text):
        p = re.sub(r"\s+", " ", part).strip()
        if len(p) < 4:
            continue

        # Config switch if chunk starts with a code.
        m_code = re.match(r"^([A-Z]{1,4}(?:-[A-Z]{1,3})?)\b", p)
        if m_code:
            current_config = m_code.group(1)

        # Strong pattern: boom m, radius m, capacity t
        for boom_s, radius_s, cap_s in TRIPLE_RE.findall(p):
            boom = parse_float(boom_s)
            radius = parse_float(radius_s)
            cap = parse_float(cap_s)
            if None in (boom, radius, cap):
                continue
            if likely_value(boom, 5, 200) and likely_value(radius, 1, 200) and likely_value(cap, 0.5, 5000):
                points.append((current_config, boom, radius, cap, p[:220]))

        # Fallback pattern: one m and one t in same chunk.
        ms = [parse_float(x) for x in NUMBER_M_RE.findall(p)]
        ts = [parse_float(x) for x in NUMBER_T_RE.findall(p)]
        ms = [x for x in ms if x is not None and likely_value(x, 5, 200)]
        ts = [x for x in ts if x is not None and likely_value(x, 0.5, 5000)]
        if ms and ts:
            points.append((current_config, ms[0], None, ts[0], p[:220]))

    # De-duplicate exact tuples.
    seen = set()
    unique = []
    for row in points:
        key = row[:4]
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def build_database():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(
        """
        DROP TABLE IF EXISTS load_chart_points;
        DROP TABLE IF EXISTS crane_configurations;
        DROP TABLE IF EXISTS crane_manuals;

        CREATE TABLE crane_manuals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            manufacturer TEXT,
            model TEXT,
            estimated_page_count INTEGER NOT NULL,
            file_size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            extracted_text_sample TEXT NOT NULL
        );

        CREATE TABLE crane_configurations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manual_id INTEGER NOT NULL,
            config_code TEXT NOT NULL,
            description TEXT NOT NULL,
            FOREIGN KEY (manual_id) REFERENCES crane_manuals(id)
        );

        CREATE TABLE load_chart_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manual_id INTEGER NOT NULL,
            config_code TEXT NOT NULL,
            boom_length_m REAL,
            radius_m REAL,
            capacity_t REAL NOT NULL,
            raw_snippet TEXT NOT NULL,
            FOREIGN KEY (manual_id) REFERENCES crane_manuals(id)
        );

        CREATE INDEX idx_cfg_manual ON crane_configurations(manual_id);
        CREATE INDEX idx_points_manual ON load_chart_points(manual_id);
        CREATE INDEX idx_points_cfg ON load_chart_points(config_code);
        """
    )

    for pdf in sorted(Path(".").glob("*.pdf")):
        data = pdf.read_bytes()
        manufacturer, model = parse_filename(pdf.name)
        text = extract_pdf_text(pdf)

        cur.execute(
            """
            INSERT INTO crane_manuals (
                file_name, manufacturer, model, estimated_page_count,
                file_size_bytes, sha256, extracted_text_sample
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pdf.name,
                manufacturer,
                model,
                estimate_page_count(data),
                len(data),
                file_sha256(data),
                text[:500],
            ),
        )
        manual_id = cur.lastrowid

        configs = extract_configurations(text)
        if not configs:
            configs = {"UNKNOWN": "Configuration not confidently identified from extracted text."}

        for code, description in sorted(configs.items()):
            cur.execute(
                "INSERT INTO crane_configurations (manual_id, config_code, description) VALUES (?, ?, ?)",
                (manual_id, code, description),
            )

        points = extract_load_points(text)
        for config_code, boom_m, radius_m, cap_t, snippet in points:
            cur.execute(
                """
                INSERT INTO load_chart_points (
                    manual_id, config_code, boom_length_m, radius_m, capacity_t, raw_snippet
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (manual_id, config_code, boom_m, radius_m, cap_t, snippet),
            )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    build_database()
    print(f"Database written to {DB_PATH}")
