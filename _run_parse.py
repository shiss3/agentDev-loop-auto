"""临时:跑 0.5.0 解析,dump spec 拆分情况。用完删。"""
import asyncio
import json
import os

os.environ["AUTOLOOP_PARSE_LOG"] = "1"

from autoloop_agent.core.architect import Governor

TEXT = """为 client-user 酒店预订前端新增三项独立业务功能,要求三项功能互不依赖、新增文件互不重叠,便于并行开发;最后统一接入路由。

一、酒店收藏夹:用户可在搜索结果页酒店卡片和酒店详情页点击收藏按钮,收藏/取消收藏酒店;新增收藏列表页查看所有已收藏酒店,点击可跳转详情页。收藏状态需本地持久化,API 封装收藏的增删查。

二、降价提醒:用户可在酒店详情页订阅某酒店的价格变动;降价后在"我的提醒"页展示提醒卡片(含原价、现价、降幅)。新增订阅/取消订阅/查询我的提醒的 API。

三、酒店评价:用户可在酒店详情页查看该酒店的评价列表(评分+文字+作者+时间);已入住用户可提交评价。新增评价提交/查询的 API。

接入:三项功能的新页面统一在路由中注册,依赖三项功能完成后再接入。"""


async def main():
    g = Governor(project_dir=r"E:\front\html\Easu\apps\client-user")
    try:
        spec = await g._parse_requirement(TEXT, context_continuation=False)
    finally:
        await g.close()
    print("\n========== SPEC ==========")
    print(json.dumps(spec, ensure_ascii=False, indent=2))
    # 模块拆分 + 分层
    from autoloop_agent.core.module_scheduler import build_execution_layers, build_module_edges, module_file_set
    req_id, modules = g._spec_to_modules(spec)
    print("\n========== MODULES ==========")
    for m in modules:
        fs = module_file_set(m)
        print(f"\n[{m['module_id']}] {m['summary'][:50]}")
        print(f"  deps: {m['deps']}")
        print(f"  files ({len(fs)}): {sorted(fs)}")
    print("\n========== EDGES ==========")
    print(build_module_edges(modules))
    print("\n========== LAYERS ==========")
    print(build_execution_layers(modules))


asyncio.run(main())
