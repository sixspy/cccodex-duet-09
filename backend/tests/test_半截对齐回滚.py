"""半截对齐回滚：中途失败收口形态 / 同票结论读 / 在途锁 / 并发不见半截现行。"""
import os
import threading
import time
from pathlib import Path

_tmp = Path(__file__).resolve().parent / "_tmp"
_tmp.mkdir(exist_ok=True)
os.environ["数据目录"] = str(_tmp)
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest

from app import seed
from app.db import 连接
from app.services import 左装载, 右装载
from app.modules.半截对齐回滚 import 服务
from app.modules.半截对齐回滚.库 import 确保库


def setup_function():
    db = _tmp / "对拍.db"
    if db.exists():
        db.unlink()
    seed.初始化库()
    确保库()


def _灌(键数: int = 5):
    """K1..Kn 双边同额；末键 Kn 仅左轨（右缺+左悬）。"""
    左装载.装一批({
        "事件": [
            {"事件编号": f"L{i}", "业务键": f"K{i:02d}", "金额分": 100 * i,
             "源键": "渠甲", "事件时": "2026-11-23T10:00:00Z"}
            for i in range(1, 键数 + 1)
        ]
    })
    右装载.装一批({
        "事件": [
            {"事件编号": f"R{i}", "业务键": f"K{i:02d}", "金额分": 100 * i,
             "源键": "核乙", "事件时": "2026-11-23T10:00:01Z"}
            for i in range(1, 键数)
        ]
    })


def test_清回形态_现行差空_结论读空集():
    _灌()
    r = 服务.跑一趟(形态="清回", 肇事键="K03")
    assert r["形态标记"] == "清回"
    assert r["结果"] == "失败"
    assert r["现行差行数"] == 0
    assert r["归档键"] == ""
    assert r["失败指纹"] != ""
    assert r["valid"] is True
    assert r["已写键数"] == 2 and r["键总数"] == 5
    # 现行可读集回到趟前空集
    assert 服务.差详情(现行=True) == []
    assert 服务.悬账列表(现行=True) == []
    # 清回残行在册可审但不读为现行
    残 = 服务.差详情()
    assert len(残) == 2 and all(x["可见"] == "清回" for x in 残)
    # 切流账册钉失败指纹
    账 = 服务.账册列表()[0]
    assert 账["失败指纹"] == r["失败指纹"]
    assert 账["清回差行数"] == 2
    # 结论读空差集（同票）
    g = 服务.跑结论(趟号=r["趟号"])
    assert g["现行差行数"] == 0
    assert g["是否通过"] is False
    assert "空差集" in g["说明"]


def test_双写形态_前半留现行_肇事在归档():
    _灌()
    r = 服务.跑一趟(形态="双写", 肇事键="K03")
    assert r["形态标记"] == "双写"
    assert r["结果"] == "失败"
    assert r["现行差行数"] == 2
    assert r["归档键"] == "K03"
    assert r["valid"] is True
    # 现行 = 前半已成功键
    assert sorted(x["业务键"] for x in 服务.差详情(现行=True)) == ["K01", "K02"]
    # 肇事键进只读归档旁路凭证，指纹同票
    归档 = 服务.归档列表()
    assert len(归档) == 1
    assert 归档[0]["业务键"] == "K03"
    assert 归档[0]["失败指纹"] == r["失败指纹"]
    # 结论读现行部分集，与清回形态可区分
    g = 服务.跑结论(趟号=r["趟号"])
    assert g["现行差行数"] == 2
    assert g["归档键"] == "K03"
    assert g["是否通过"] is False
    assert "部分" in g["说明"]


def test_纯成功趟_全量现行():
    _灌()
    r = 服务.跑一趟(形态="清回")
    assert r["形态标记"] == "成功"
    assert r["结果"] == "成功"
    assert r["现行差行数"] == 5
    assert r["valid"] is True
    assert len(服务.差详情(现行=True)) == 5
    assert len(服务.悬账列表(现行=True)) == 1  # K5 仅左 → 左悬
    g = 服务.跑结论(趟号=r["趟号"])
    assert g["是否通过"] is True
    assert g["现行差行数"] == 5


def test_清回后再跑_不见半截现行():
    _灌()
    服务.跑一趟(形态="清回", 肇事键="K03")
    assert 服务.差详情(现行=True) == []
    r2 = 服务.跑一趟(形态="双写")  # 纯成功趟
    assert r2["形态标记"] == "成功"
    assert r2["现行差行数"] == 5
    assert sorted(x["业务键"] for x in 服务.差详情(现行=True)) == [
        "K01", "K02", "K03", "K04", "K05",
    ]


def test_在途锁_拒第二趟_防双成功():
    _灌()
    进入 = threading.Event()
    放行 = threading.Event()

    def 钩子(k):
        if not 进入.is_set():
            进入.set()
            assert 放行.wait(5)

    盒 = {}

    def 跑甲():
        盒["甲"] = 服务.跑一趟(形态="清回", 肇事键="K04", 每键钩子=钩子)

    t = threading.Thread(target=跑甲)
    t.start()
    assert 进入.wait(5)
    with pytest.raises(服务.在途冲突):
        服务.跑一趟(形态="清回")
    放行.set()
    t.join(5)
    assert 盒["甲"]["结果"] == "失败"
    assert len(服务.趟列表()) == 1  # 只有甲一趟,未双成功


def test_收口与二次对齐并发_不见半截现行():
    _灌(键数=30)
    停 = threading.Event()
    观察 = []

    def 读():
        while not 停.is_set():
            观察.append(len(服务.差详情(现行=True)))

    def 钩子(k):
        time.sleep(0)  # 让出时间片,促读写交错

    t = threading.Thread(target=读)
    t.start()
    r = 服务.跑一趟(形态="双写", 肇事键="K15", 每键钩子=钩子)
    停.set()
    t.join(5)
    终 = r["现行差行数"]
    assert 终 == 14  # K1..K14 前半留现行
    assert r["valid"] is True
    assert 观察, "读线程未采到样"
    assert all(n in (0, 终) for n in 观察), f"出现半截现行:{sorted(set(观察))}"
    # 收口后二次对齐:干净全量,不见上半截
    r2 = 服务.跑一趟(形态="清回")
    assert r2["形态标记"] == "成功"
    assert r2["现行差行数"] == 30


def test_收口只读接口字段():
    _灌()
    r = 服务.跑一趟(形态="双写", 肇事键="K02")
    for 字段 in ("趟号", "现行差行数", "归档键", "形态标记", "valid"):
        assert 字段 in r
    列 = 服务.收口列表()
    assert len(列) == 1 and 列[0]["趟号"] == r["趟号"]
    单 = 服务.收口报告(r["趟号"])
    assert 单["valid"] is True and 单["归档键"] == "K02"


def test_未收口趟_结论拒读_拒开新趟():
    _灌()
    确保库()
    conn = 连接()
    conn.execute(
        "INSERT INTO 半回_趟(形态,状态,开始时刻) VALUES ('清回','在途','2026-11-24T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(服务.在途冲突):
        服务.跑结论(趟号=1)
    with pytest.raises(服务.在途冲突):
        服务.跑一趟()


def test_失败两形态_结论读集可区分():
    # 清回趟:结论读空集
    _灌()
    r清 = 服务.跑一趟(形态="清回", 肇事键="K03")
    g清 = 服务.跑结论(趟号=r清["趟号"])
    assert g清["现行差行数"] == 0 and g清["归档键"] == ""
    # 重置后再造双写趟:结论读前半部分集
    db = _tmp / "对拍.db"
    db.unlink()
    seed.初始化库()
    确保库()
    _灌()
    r双 = 服务.跑一趟(形态="双写", 肇事键="K03")
    g双 = 服务.跑结论(趟号=r双["趟号"])
    assert g双["现行差行数"] == 2 and g双["归档键"] == "K03"
    assert g清["现行差行数"] != g双["现行差行数"]


def test_收口报告不随后续趟漂移():
    _灌()
    r1 = 服务.跑一趟(形态="双写", 肇事键="K03")
    assert r1["valid"] is True
    r2 = 服务.跑一趟(形态="清回")  # 纯成功趟
    assert r2["形态标记"] == "成功"
    # 旧双写趟收口报告仍可复核:现行按趟永久落定,不翻旧账
    再读 = 服务.收口报告(r1["趟号"])
    assert 再读["valid"] is True
    assert 再读["现行差行数"] == 2
    # 当前可读集 = 最新趟全量;同票结论重读旧趟集合不变
    assert len(服务.差详情(现行=True)) == 5
    g1 = 服务.跑结论(趟号=r1["趟号"])
    assert g1["现行差行数"] == 2
