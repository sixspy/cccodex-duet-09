"""半截对齐回滚:模块路由。前缀 /api/半截对齐回滚,与基线路由零冲突。"""
from fastapi import APIRouter, HTTPException
from . import 服务

路由 = APIRouter(prefix="/api/半截对齐回滚", tags=["半截对齐回滚"])


@路由.post("/跑")
def api_跑(体: dict | None = None):
    体 = 体 or {}
    try:
        return 服务.跑一趟(
            形态=str(体.get("形态") or "清回"),
            肇事键=str(体.get("肇事键") or ""),
        )
    except 服务.在途冲突 as e:
        raise HTTPException(409, str(e))


@路由.post("/结论")
def api_结论(体: dict | None = None):
    体 = 体 or {}
    趟号 = 体.get("趟号")
    try:
        return 服务.跑结论(趟号=int(趟号) if 趟号 is not None else None)
    except 服务.趟不存在 as e:
        raise HTTPException(404, str(e))
    except 服务.在途冲突 as e:
        raise HTTPException(409, str(e))


@路由.get("/收口")
def api_收口列表():
    return 服务.收口列表()


@路由.get("/收口/{tid}")
def api_收口(tid: int):
    try:
        return 服务.收口报告(tid)
    except 服务.趟不存在 as e:
        raise HTTPException(404, str(e))


@路由.get("/趟")
def api_趟():
    return 服务.趟列表()


@路由.get("/差")
def api_差(现行: bool = False, 趟号: int | None = None):
    return 服务.差详情(现行=现行, 趟号=趟号)


@路由.get("/悬账")
def api_悬账(现行: bool = False):
    return 服务.悬账列表(现行=现行)


@路由.get("/归档")
def api_归档():
    return 服务.归档列表()


@路由.get("/切流账册")
def api_账册():
    return 服务.账册列表()


@路由.get("/结论")
def api_结论列表():
    return 服务.结论列表()
