"""本地知识库的命令行入口。

建库、加文档、重建索引都会跑解析和 embedding，一份论文集是分钟级的活。这个入口和
管理界面共用同一套 service，适合批量导入目录，以及给评测准备多版索引。

    uv run python -m app.cli.kb list
    uv run python -m app.cli.kb create papers --name "我的论文库"
    uv run python -m app.cli.kb add papers ~/papers/*.pdf
    uv run python -m app.cli.kb rebuild papers      # 换 embedding 模型之后
    uv run python -m app.cli.kb version create papers --id candidate --engine bm25 --no-activate
    uv run python -m app.cli.kb version list papers
    uv run python -m app.cli.kb version activate papers candidate
    uv run python -m app.cli.kb search papers "RRF 怎么融合"
    uv run python -m app.cli.kb delete papers
"""

import argparse
import asyncio
from pathlib import Path

from app.core.config import Settings
from app.knowledge_contracts import KnowledgeUnavailableError, RagSearchRequest
from app.rag.kb import KbManifest, KbNameError, local_kb_service
from app.rag.kb.service import expand_sources


def _progress(stage: str, done: int, total: int) -> None:
    print(f"  [{done}/{total}] {stage}", flush=True)


def _print_manifest(manifest: KbManifest) -> None:
    active = manifest.active
    if active is not None:
        state = f"{active.describe()}（共 {len(manifest.versions)} 版）"
    elif manifest.has_legacy_layout:
        state = "（旧布局，需 rebuild 迁移）"
    else:
        state = "（未建索引）"
    print(f"{manifest.slug}\t{manifest.name}\t{len(manifest.documents)} 篇\t{state}")


def _print_versions(manifest: KbManifest) -> None:
    if not manifest.versions:
        state = "旧布局，先运行 rebuild" if manifest.has_legacy_layout else "还没有索引版本"
        print(f"{manifest.slug}：{state}")
        return
    for version in manifest.versions:
        flags = ["active"] if version.version_id == manifest.active_version else []
        if not version.covers(manifest.document_hashes):
            flags.append("stale")
        state = f" [{','.join(flags)}]" if flags else ""
        print(
            f"{version.version_id}{state}\t{version.label}\t{version.embedding.describe()}"
            f"\t{version.retrieval.describe()}\t{version.node_count} 节点"
        )


async def _run(args: argparse.Namespace) -> int:
    service = local_kb_service(Settings())

    if args.command == "list":
        manifests = service.list_kbs()
        if not manifests:
            print(f"{service.root} 下还没有知识库。用 create 建一个。")
            return 0
        for manifest in manifests:
            _print_manifest(manifest)
        return 0

    if args.command == "create":
        _print_manifest(service.create(args.name or args.slug, slug=args.slug))
        return 0

    if args.command == "delete":
        print("已删除" if service.delete(args.slug) else f"知识库 {args.slug} 不存在")
        return 0

    if args.command == "add":
        sources = expand_sources(args.paths)
        if not sources:
            print("给的路径里没有可导入的文件（支持 .pdf/.md/.markdown/.txt）。")
            return 1
        print(f"待导入 {len(sources)} 个文件…")
        result = await service.add_documents(
            args.slug, sources, skip_failures=args.skip_failures, progress=_progress
        )
        for item in result.skipped:
            print(f"跳过 {item.filename}：{item.reason}")
        print(f"新增 {len(result.added)} 篇。")
        _print_manifest(result.manifest)
        return 1 if result.skipped and not result.added else 0

    if args.command == "rebuild":
        _print_manifest(await service.rebuild(args.slug, progress=_progress))
        return 0

    if args.command == "version":
        if args.version_command == "list":
            _print_versions(service.get(args.slug))
            return 0
        if args.version_command == "create":
            manifest, version = await service.create_version(
                args.slug,
                version_id=args.version_id,
                label=args.label,
                engine=args.engine,
                activate=not args.no_activate,
                progress=_progress,
            )
            print(f"已创建索引版本 {version.version_id}。")
            _print_versions(manifest)
            return 0
        if args.version_command == "activate":
            manifest = service.activate_version(args.slug, args.version_id)
            print(f"已激活索引版本 {args.version_id}。")
            _print_versions(manifest)
            return 0
        manifest = service.delete_version(args.slug, args.version_id)
        print(f"已删除索引版本 {args.version_id}。")
        _print_versions(manifest)
        return 0

    # search
    bundle = await service.search(
        None,
        RagSearchRequest(
            query=args.query,
            top_k=args.top_k,
            kb_slug=args.slug,
            kb_version_id=args.version_id,
        ),
    )
    if not bundle.evidence:
        print("没有命中。")
        return 0
    for segment in bundle.evidence:
        pages = [str(item["page_no"]) for item in segment.locations if item.get("page_no")]
        where = f" p.{','.join(pages)}" if pages else ""
        print(f"[{segment.citation_id}] {segment.title}{where}")
        print(f"    {segment.quote[:300]}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地知识库管理")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出所有知识库")

    create = sub.add_parser("create", help="新建一个空知识库")
    create.add_argument("slug", help="目录标识：小写字母、数字、连字符")
    create.add_argument("--name", default=None, help="显示名，默认与 slug 相同")

    delete = sub.add_parser("delete", help="删除知识库及其索引")
    delete.add_argument("slug")

    add = sub.add_parser("add", help="加入文档并重建索引")
    add.add_argument("slug")
    # 在这里就展开成 Path：`~` 由 shell 展开，但 `--` 之后或加了引号的路径不会。
    add.add_argument(
        "paths",
        nargs="+",
        type=lambda value: Path(value).expanduser(),
        help=".pdf/.md/.markdown/.txt，或包含它们的目录（递归展开）",
    )
    add.add_argument(
        "--skip-failures",
        action="store_true",
        help="跳过解析失败的文件继续导入，而不是整批退回",
    )

    rebuild = sub.add_parser("rebuild", help="按清单里的源路径重新解析并建索引")
    rebuild.add_argument("slug")

    version = sub.add_parser("version", help="列出、创建、激活或删除索引版本")
    version_sub = version.add_subparsers(dest="version_command", required=True)

    version_list = version_sub.add_parser("list", help="列出一份知识库的索引版本")
    version_list.add_argument("slug")

    version_create = version_sub.add_parser("create", help="在当前文档集合上创建新版索引")
    version_create.add_argument("slug")
    version_create.add_argument(
        "--id", dest="version_id", default=None, help="版本标识，默认自动生成 vN"
    )
    version_create.add_argument("--label", default="", help="给人看的版本说明")
    version_create.add_argument("--engine", choices=("hybrid", "dense", "bm25"), default="hybrid")
    version_create.add_argument(
        "--no-activate", action="store_true", help="只建版本，不切换当前检索版本"
    )

    version_activate = version_sub.add_parser("activate", help="切换默认检索版本")
    version_activate.add_argument("slug")
    version_activate.add_argument("version_id")

    version_delete = version_sub.add_parser("delete", help="删除索引版本（最后一版不能删）")
    version_delete.add_argument("slug")
    version_delete.add_argument("version_id")

    search = sub.add_parser("search", help="在知识库里检索，用来确认索引是好的")
    search.add_argument("slug")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument(
        "--version", dest="version_id", default=None, help="指定索引版本，默认用 active"
    )

    return parser.parse_args()


def main() -> None:
    try:
        code = asyncio.run(_run(_parse_args()))
    except (KnowledgeUnavailableError, KbNameError) as error:
        # 这些消息按约束 4 就是写给人/模型看的可执行指令，原样打出来，不要栈。
        print(f"错误：{error}")
        raise SystemExit(1) from None
    raise SystemExit(code)


if __name__ == "__main__":
    main()
