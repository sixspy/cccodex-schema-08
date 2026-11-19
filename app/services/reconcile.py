from app.db import get_db
from app.services.contracts import catalog_pointer, current_page
from app.services.timeutil import iso, today_watermark


def _line(conn, table_name: str, contract_ver: int | None) -> dict:
    pointer = catalog_pointer(conn, table_name)
    page = current_page(conn, table_name)
    page_ver = page["version_no"] if page else None
    aligned = contract_ver is not None and contract_ver == pointer == page_ver
    if aligned:
        detail = f"契约v{contract_ver}/目录v{pointer}/附页v{page_ver} 同版"
    elif contract_ver is None:
        detail = f"契约无现行/目录v{pointer}/附页v{page_ver} 对不上"
    else:
        detail = f"契约v{contract_ver}/目录v{pointer}/附页v{page_ver} 对不上"
    return {
        "table_name": table_name,
        "contract_version": contract_ver,
        "catalog_version": pointer,
        "page_version": page_ver,
        "aligned": aligned,
        "detail": detail,
    }


def run_reconcile(as_of: str, db_path: str | None = None) -> dict:
    """对账水线：契约版 / 目录版 / 附页版三处对照，同一版号才算对齐。"""
    with get_db(db_path) as conn:
        contract_vers = {
            r["table_name"]: r["version_no"]
            for r in conn.execute(
                "SELECT table_name, version_no FROM schema_contracts WHERE status='现行'"
            )
        }
        catalog_tables = [
            r["table_name"]
            for r in conn.execute("SELECT table_name FROM catalog_tables ORDER BY table_name")
        ]
        # 契约有现行但目录没登记，也是漂移，不能漏。
        names = sorted(set(contract_vers) | set(catalog_tables))
        watermark = as_of or today_watermark()
        lines = [_line(conn, name, contract_vers.get(name)) for name in names]
        if not lines:
            lines = []
        matched = sum(1 for li in lines if li["aligned"])
        drifted = len(lines) - matched
        summary = (
            f"对账{watermark}：对齐{matched}，漂移{drifted}"
            + (("；" + "，".join(li["detail"] for li in lines if not li["aligned"]))
               if drifted else "")
        )
        cur = conn.execute(
            "INSERT INTO reconcile_reports(as_of, watermark, matched, drifted, summary) "
            "VALUES (?,?,?,?,?)",
            (as_of or watermark, watermark, matched, drifted, summary),
        )
        report_id = cur.lastrowid
        for li in lines:
            conn.execute(
                "INSERT INTO reconcile_lines(report_id, table_name, contract_version, "
                "catalog_version, page_version, aligned, detail) VALUES (?,?,?,?,?,?,?)",
                (
                    report_id,
                    li["table_name"],
                    li["contract_version"],
                    li["catalog_version"],
                    li["page_version"],
                    1 if li["aligned"] else 0,
                    li["detail"],
                ),
            )
        row = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (report_id,)
        ).fetchone()
        report = dict(row)
        report["created_at"] = iso(row["created_at"])
        report["lines"] = lines
        return report


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["created_at"] = iso(r["created_at"])
            lines = []
            for li in conn.execute(
                "SELECT * FROM reconcile_lines WHERE report_id=? ORDER BY table_name",
                (r["id"],),
            ).fetchall():
                line = dict(li)
                line["aligned"] = bool(li["aligned"])
                lines.append(line)
            item["lines"] = lines
            out.append(item)
        return out
