"""半截对齐回滚：键对齐编排事务收口。

一趟 = 开票(趟号,同票) → 逐键写在途差/悬(逐键落库,可读集不见) → 收口(单事务,报告与可读集同成败):
  成功    : 在途→现行 全量
  失败·清回: 本趟在途整趟清回,现行回到趟前集合,切流账册钉失败指纹,结论读空差集
  失败·双写: 已成功键在途→现行(前半留现行),肇事键进只读归档旁路凭证,结论读现行部分集

现行按趟永久落定,不翻旧账;「当前可读集」= 最新一个有现行差的趟的现行行,
故收口报告随时可复核(valid 不随后续趟漂移),再跑对齐也只读最新趟,不见半截。

硬约束落点:
  不加锁会双成功      → 开票 BEGIN IMMEDIATE + 在途趟检查,并发第二趟拒 409
  收口报告可读集同成败 → 收口全部写入与账册同事务提交
  再跑不见半截现行    → 读口径只认最新趟的 可见='现行';在途/清回一律不读
  中途失败不收口脏读  → 任何异常都转失败并强制收口,不留未收口在途趟
"""
from __future__ import annotations
import hashlib
from app.db import 连接
from app.seed import 此刻
from app.services import 目录
from .库 import 确保库

形态集 = ("清回", "双写")


class 在途冲突(Exception):
    """已有在途趟未收口,拒开新趟(防双成功)。"""


class 趟不存在(Exception):
    pass


def _连接():
    确保库()
    conn = 连接()
    conn.isolation_level = None  # 手动事务:显式 BEGIN/COMMIT/ROLLBACK
    return conn


def _指纹(肇事键: str, 原因: str, 已写键: list[str]) -> str:
    料 = "|".join([肇事键, 原因, ",".join(已写键)])
    return hashlib.sha256(料.encode("utf-8")).hexdigest()[:12]


def _源汇总(conn) -> tuple[dict, dict, dict, dict]:
    左汇总: dict[str, int] = {}
    左代表: dict[str, str] = {}
    for r in conn.execute("SELECT * FROM 左轨流水 WHERE 命运='已入左'").fetchall():
        k = r["业务键"]
        左汇总[k] = 左汇总.get(k, 0) + int(r["金额分"])
        左代表[k] = r["事件编号"]
    右汇总: dict[str, int] = {}
    右代表: dict[str, str] = {}
    for r in conn.execute("SELECT * FROM 右轨流水 WHERE 命运='已入右'").fetchall():
        k = r["业务键"]
        右汇总[k] = 右汇总.get(k, 0) + int(r["金额分"])
        右代表[k] = r["事件编号"]
    return 左汇总, 左代表, 右汇总, 右代表


def _开票(conn, 形态: str) -> int:
    """BEGIN IMMEDIATE 取写锁 + 在途检查:不加锁会双成功。"""
    conn.execute("BEGIN IMMEDIATE")
    在途 = conn.execute(
        "SELECT COUNT(*) AS c FROM 半回_趟 WHERE 状态='在途'"
    ).fetchone()["c"]
    if 在途:
        conn.execute("ROLLBACK")
        raise 在途冲突("已有在途趟未收口,拒开新趟")
    cur = conn.execute(
        "INSERT INTO 半回_趟(形态,状态,开始时刻) VALUES (?,'在途',?)",
        (形态, 此刻()),
    )
    趟号 = cur.lastrowid
    conn.execute("COMMIT")
    return 趟号


def _写一键(conn, 趟号: int, k: str, 左汇总, 左代表, 右汇总, 右代表, 容差: int) -> None:
    """单键落差+悬账,逐键落库(模拟写到一半抛错会留下在途残行)。"""
    时刻 = 此刻()
    有左, 有右 = k in 左汇总, k in 右汇总
    conn.execute("BEGIN")
    if 有左 and 有右:
        la, ra = 左汇总[k], 右汇总[k]
        if abs(la - ra) <= 容差:
            conn.execute(
                "INSERT INTO 半回_差(趟号,业务键,左金额分,右金额分,差型,说明,可见,时刻)"
                " VALUES (?,?,?,?,?,?,'在途',?)",
                (趟号, k, la, ra, "对齐", "双边金额在容差内", 时刻),
            )
        else:
            conn.execute(
                "INSERT INTO 半回_差(趟号,业务键,左金额分,右金额分,差型,说明,可见,时刻)"
                " VALUES (?,?,?,?,?,?,'在途',?)",
                (趟号, k, la, ra, "金额不符", f"左{la}-右{ra}", 时刻),
            )
    elif 有左:
        conn.execute(
            "INSERT INTO 半回_差(趟号,业务键,左金额分,右金额分,差型,说明,可见,时刻)"
            " VALUES (?,?,?,?,?,?,'在途',?)",
            (趟号, k, 左汇总[k], None, "右缺", "仅左轨有键", 时刻),
        )
        conn.execute(
            "INSERT INTO 半回_悬账(趟号,业务键,悬边,金额分,事件编号,原因,可见,时刻)"
            " VALUES (?,?,?,?,?,?,'在途',?)",
            (趟号, k, "左", 左汇总[k], 左代表.get(k, ""), "右轨缺键", 时刻),
        )
    else:
        conn.execute(
            "INSERT INTO 半回_差(趟号,业务键,左金额分,右金额分,差型,说明,可见,时刻)"
            " VALUES (?,?,?,?,?,?,'在途',?)",
            (趟号, k, None, 右汇总[k], "左缺", "仅右轨有键", 时刻),
        )
        conn.execute(
            "INSERT INTO 半回_悬账(趟号,业务键,悬边,金额分,事件编号,原因,可见,时刻)"
            " VALUES (?,?,?,?,?,?,'在途',?)",
            (趟号, k, "右", 右汇总[k], 右代表.get(k, ""), "左轨缺键", 时刻),
        )
    conn.execute("COMMIT")


def _收口(conn, 趟号: int, 形态: str, 失败: tuple[str, str] | None, 已写键: list[str],
          左汇总, 右汇总, 键总数: int) -> None:
    """单事务收口:报告(切流账册)与可读集合同成败。"""
    时刻 = 此刻()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if 失败 is None:
            conn.execute("UPDATE 半回_差 SET 可见='现行' WHERE 趟号=?", (趟号,))
            conn.execute("UPDATE 半回_悬账 SET 可见='现行' WHERE 趟号=?", (趟号,))
            现行差 = conn.execute(
                "SELECT COUNT(*) AS c FROM 半回_差 WHERE 趟号=? AND 可见='现行'", (趟号,)
            ).fetchone()["c"]
            现行悬 = conn.execute(
                "SELECT COUNT(*) AS c FROM 半回_悬账 WHERE 趟号=? AND 可见='现行'", (趟号,)
            ).fetchone()["c"]
            conn.execute(
                "INSERT INTO 半回_切流账册(趟号,形态,现行差行数,现行悬账数,时刻)"
                " VALUES (?,'成功',?,?,?)",
                (趟号, 现行差, 现行悬, 时刻),
            )
            conn.execute(
                "UPDATE 半回_趟 SET 状态='已收口',结果='成功',键总数=?,已写键数=?,收口时刻=?"
                " WHERE 趟号=?",
                (键总数, len(已写键), 时刻, 趟号),
            )
        else:
            肇事键, 原因 = 失败
            指纹 = _指纹(肇事键, 原因, 已写键)
            if 形态 == "清回":
                # 整趟清回:本趟在途全部转清回,现行不动,回到趟前集合
                conn.execute(
                    "UPDATE 半回_差 SET 可见='清回' WHERE 趟号=? AND 可见='在途'", (趟号,)
                )
                conn.execute(
                    "UPDATE 半回_悬账 SET 可见='清回' WHERE 趟号=? AND 可见='在途'", (趟号,)
                )
                清回差 = conn.execute(
                    "SELECT COUNT(*) AS c FROM 半回_差 WHERE 趟号=? AND 可见='清回'", (趟号,)
                ).fetchone()["c"]
                清回悬 = conn.execute(
                    "SELECT COUNT(*) AS c FROM 半回_悬账 WHERE 趟号=? AND 可见='清回'", (趟号,)
                ).fetchone()["c"]
                conn.execute(
                    "INSERT INTO 半回_切流账册(趟号,形态,失败指纹,现行差行数,现行悬账数,"
                    "清回差行数,清回悬账数,时刻) VALUES (?,'清回',?,0,0,?,?,?)",
                    (趟号, 指纹, 清回差, 清回悬, 时刻),
                )
            else:
                # 双写:已成功键在途→现行(前半留现行),肇事键进只读归档旁路凭证
                conn.execute(
                    "UPDATE 半回_差 SET 可见='现行' WHERE 趟号=? AND 可见='在途'", (趟号,)
                )
                conn.execute(
                    "UPDATE 半回_悬账 SET 可见='现行' WHERE 趟号=? AND 可见='在途'", (趟号,)
                )
                现行差 = conn.execute(
                    "SELECT COUNT(*) AS c FROM 半回_差 WHERE 趟号=? AND 可见='现行'", (趟号,)
                ).fetchone()["c"]
                现行悬 = conn.execute(
                    "SELECT COUNT(*) AS c FROM 半回_悬账 WHERE 趟号=? AND 可见='现行'", (趟号,)
                ).fetchone()["c"]
                conn.execute(
                    "INSERT INTO 半回_归档(趟号,业务键,左金额分,右金额分,失败指纹,说明,时刻)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (趟号, 肇事键, 左汇总.get(肇事键), 右汇总.get(肇事键), 指纹,
                     f"肇事键旁路:{原因}", 时刻),
                )
                conn.execute(
                    "INSERT INTO 半回_切流账册(趟号,形态,失败指纹,归档键,现行差行数,现行悬账数,时刻)"
                    " VALUES (?,'双写',?,?,?,?,?)",
                    (趟号, 指纹, 肇事键, 现行差, 现行悬, 时刻),
                )
            conn.execute(
                "UPDATE 半回_趟 SET 状态='已收口',结果='失败',键总数=?,已写键数=?,肇事键=?,"
                "失败指纹=?,收口时刻=? WHERE 趟号=?",
                (键总数, len(已写键), 肇事键, 指纹, 时刻, 趟号),
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def 跑一趟(形态: str = "清回", 肇事键: str = "", 每键钩子=None) -> dict:
    """键对齐编排:开票→逐键在途→收口。肇事键非空时在该键注入抛错。"""
    形态 = 形态 if 形态 in 形态集 else "清回"
    conn = _连接()
    try:
        趟号 = _开票(conn, 形态)
        容差 = int(目录.轨规().get("金额容差分", "0"))
        左汇总, 左代表, 右汇总, 右代表 = _源汇总(conn)
        所有键 = sorted(set(左汇总) | set(右汇总))

        已写键: list[str] = []
        失败: tuple[str, str] | None = None
        for k in 所有键:
            if 肇事键 and k == 肇事键:
                失败 = (k, f"键{k}落差写入抛错(注入)")
                break
            try:
                _写一键(conn, 趟号, k, 左汇总, 左代表, 右汇总, 右代表, 容差)
            except Exception as e:  # 中途失败必须收口,否则脏读
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                失败 = (k, f"键{k}落差写入抛错:{e}")
                break
            已写键.append(k)
            if 每键钩子:
                每键钩子(k)

        _收口(conn, 趟号, 形态, 失败, 已写键, 左汇总, 右汇总, len(所有键))
        return 收口报告(趟号)
    finally:
        conn.close()


def 跑结论(趟号: int | None = None) -> dict:
    """对拍结论:读引用同票(趟号),失败趟读到的差行集合按形态可区分。"""
    conn = _连接()
    try:
        if 趟号 is None:
            行 = conn.execute(
                "SELECT 趟号 FROM 半回_趟 WHERE 状态='已收口' ORDER BY 趟号 DESC LIMIT 1"
            ).fetchone()
            if not 行:
                raise 趟不存在("尚无已收口趟")
            趟号 = 行["趟号"]
        趟 = conn.execute("SELECT * FROM 半回_趟 WHERE 趟号=?", (趟号,)).fetchone()
        if not 趟:
            raise 趟不存在(f"趟{趟号}不存在")
        if 趟["状态"] != "已收口":
            raise 在途冲突(f"趟{趟号}未收口,结论拒读(防脏读)")
        账 = conn.execute(
            "SELECT * FROM 半回_切流账册 WHERE 趟号=?", (趟号,)
        ).fetchone()
        差行 = conn.execute(
            "SELECT * FROM 半回_差 WHERE 趟号=? AND 可见='现行' ORDER BY 业务键", (趟号,)
        ).fetchall()
        悬行 = conn.execute(
            "SELECT * FROM 半回_悬账 WHERE 趟号=? AND 可见='现行' ORDER BY 业务键", (趟号,)
        ).fetchall()
        归档键 = ",".join(
            r["业务键"]
            for r in conn.execute(
                "SELECT 业务键 FROM 半回_归档 WHERE 趟号=? ORDER BY 业务键", (趟号,)
            ).fetchall()
        )
        形态 = 账["形态"] if 账 else 趟["形态"]
        自洽 = bool(账) and len(差行) == 账["现行差行数"] and 归档键 == 账["归档键"]
        通过 = 自洽 and 趟["结果"] == "成功"
        if 形态 == "清回":
            读集 = "空差集"
        elif 形态 == "双写":
            读集 = "现行部分集"
        else:
            读集 = "全量现行集"
        说明 = (
            f"趟{趟号} 形态{形态} 读{读集}:现行差{len(差行)}行/悬{len(悬行)}行"
            f"/归档[{归档键 or '-'}] 指纹:{趟['失败指纹'] or '-'}"
            + ("" if 自洽 else " 报告与可读集不符!")
        )
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO 半回_结论(趟号,形态,现行差行数,悬账数,归档键,是否通过,说明,时刻)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (趟号, 形态, len(差行), len(悬行), 归档键, 1 if 通过 else 0, 说明, 此刻()),
        )
        conn.execute("COMMIT")
        return {
            "趟号": 趟号,
            "形态": 形态,
            "现行差行数": len(差行),
            "悬账数": len(悬行),
            "归档键": 归档键,
            "是否通过": 通过,
            "说明": 说明,
        }
    finally:
        conn.close()


def 收口报告(趟号: int) -> dict:
    """对齐收口只读口径:趟号/现行差行数/归档键/形态标记/valid。"""
    conn = _连接()
    try:
        账 = conn.execute(
            "SELECT * FROM 半回_切流账册 WHERE 趟号=?", (趟号,)
        ).fetchone()
        if not 账:
            raise 趟不存在(f"趟{趟号}无收口账册")
        趟 = conn.execute("SELECT * FROM 半回_趟 WHERE 趟号=?", (趟号,)).fetchone()
        实际现行 = conn.execute(
            "SELECT COUNT(*) AS c FROM 半回_差 WHERE 趟号=? AND 可见='现行'", (趟号,)
        ).fetchone()["c"]
        实际清回 = conn.execute(
            "SELECT COUNT(*) AS c FROM 半回_差 WHERE 趟号=? AND 可见='清回'", (趟号,)
        ).fetchone()["c"]
        实际归档 = ",".join(
            r["业务键"]
            for r in conn.execute(
                "SELECT 业务键 FROM 半回_归档 WHERE 趟号=? ORDER BY 业务键", (趟号,)
            ).fetchall()
        )
        形态 = 账["形态"]
        valid = 实际现行 == 账["现行差行数"] and 实际归档 == 账["归档键"]
        if 形态 == "清回":
            valid = valid and 账["现行差行数"] == 0 and 账["失败指纹"] != "" \
                and 实际清回 == 账["清回差行数"]
        elif 形态 == "双写":
            valid = valid and 账["归档键"] != "" and 账["失败指纹"] != ""
        else:
            valid = valid and 账["失败指纹"] == "" and 账["归档键"] == ""
        return {
            "趟号": 趟号,
            "形态标记": 形态,
            "现行差行数": 账["现行差行数"],
            "归档键": 账["归档键"],
            "失败指纹": 账["失败指纹"],
            "valid": bool(valid),
            "结果": 趟["结果"] if 趟 else "",
            "键总数": 趟["键总数"] if 趟 else 0,
            "已写键数": 趟["已写键数"] if 趟 else 0,
            "清回差行数": 账["清回差行数"],
            "时刻": 账["时刻"],
        }
    finally:
        conn.close()


def 收口列表() -> list[dict]:
    conn = _连接()
    try:
        趟号s = [
            r["趟号"]
            for r in conn.execute("SELECT 趟号 FROM 半回_切流账册 ORDER BY 趟号 DESC").fetchall()
        ]
    finally:
        conn.close()
    return [收口报告(t) for t in 趟号s]


def 趟列表() -> list[dict]:
    conn = _连接()
    try:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM 半回_趟 ORDER BY 趟号 DESC").fetchall()
        ]
    finally:
        conn.close()


def _当前趟号(conn) -> int | None:
    """当前可读集归属:最新一个有现行差的趟(清回趟不产生现行,自然回退到趟前)。"""
    行 = conn.execute(
        "SELECT MAX(趟号) AS t FROM 半回_差 WHERE 可见='现行'"
    ).fetchone()
    return 行["t"] if 行 and 行["t"] is not None else None


def 差详情(现行: bool = False, 趟号: int | None = None) -> list[dict]:
    sql = "SELECT * FROM 半回_差 WHERE 1=1"
    参: list = []
    conn = _连接()
    try:
        if 现行:
            t = _当前趟号(conn)
            if t is None:
                return []
            sql += " AND 可见='现行' AND 趟号=?"
            参.append(t)
        if 趟号 is not None:
            sql += " AND 趟号=?"
            参.append(趟号)
        sql += " ORDER BY 编号 DESC"
        return [dict(r) for r in conn.execute(sql, 参).fetchall()]
    finally:
        conn.close()


def 悬账列表(现行: bool = False) -> list[dict]:
    sql = "SELECT * FROM 半回_悬账"
    参: list = []
    conn = _连接()
    try:
        if 现行:
            t = _当前趟号(conn)
            if t is None:
                return []
            sql += " WHERE 可见='现行' AND 趟号=?"
            参.append(t)
        sql += " ORDER BY 编号 DESC"
        return [dict(r) for r in conn.execute(sql, 参).fetchall()]
    finally:
        conn.close()


def 归档列表() -> list[dict]:
    conn = _连接()
    try:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM 半回_归档 ORDER BY 编号 DESC").fetchall()
        ]
    finally:
        conn.close()


def 账册列表() -> list[dict]:
    conn = _连接()
    try:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM 半回_切流账册 ORDER BY 趟号 DESC").fetchall()
        ]
    finally:
        conn.close()


def 结论列表() -> list[dict]:
    conn = _连接()
    try:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM 半回_结论 ORDER BY 编号 DESC").fetchall()
        ]
    finally:
        conn.close()
