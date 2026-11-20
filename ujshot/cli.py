"""CLI entry point.

    ujshot run   --flow flows/smoke.json [--device …] [--out …]
    ujshot check --image shot.png --expect 设置 --must-not CNY
    ujshot board --read <dir> [--status rejected]
    ujshot devices
    ujshot init  [--name my-flow]
    ujshot doctor

Every subcommand exits non-zero on a real problem, so any of them can be used
directly as a CI step.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import __version__, board, config, flow as flowmod, gallery, ocr, runner
from . import device as dev
from .types import Device


def _print_version(_args) -> None:
    print(f"ujshot {__version__}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ujshot",
        description="iOS 模拟器界面走查自动化：点进去 → 截图 → AX 树 → OCR → HTML 图册")
    parser.add_argument("--version", action="version", version=f"ujshot {__version__}")
    parser.add_argument("-C", "--dir", default=".", help="在哪个目录找 ujshot.toml（默认当前目录）")
    sub = parser.add_subparsers(dest="cmd")

    # ---- run
    p_run = sub.add_parser("run", help="跑一条流程")
    p_run.add_argument("--flow", required=True, help="流程 JSON，可只写名字（自动在 flow_dir 里找）")
    p_run.add_argument("--device", help="模拟器名或 UDID（覆盖配置）")
    p_run.add_argument("--bundle", help="Bundle ID（覆盖配置）")
    p_run.add_argument("--out", help="输出目录（覆盖配置）")
    p_run.add_argument("--app", help="先安装这个 .app")
    p_run.add_argument("--seed", help="覆盖 App 容器里的状态文件")
    p_run.add_argument("--seed-to", help="种子文件在容器内的相对路径（覆盖配置的 app.state_path）")
    p_run.add_argument("--appearance", choices=["dark", "light"], help="外观（覆盖配置）")
    p_run.add_argument("--wait-scale", type=float, default=1.0, help="整体等待倍率，慢机器用 1.5")
    p_run.add_argument("--open", action="store_true", help="跑完打开图册")
    p_run.add_argument("--no-restart", action="store_true", help="不重启 App（从当前界面开始）")
    p_run.add_argument("--dry-run", action="store_true", help="只校验流程与设备，不执行")
    p_run.add_argument("--keep-going", action="store_true",
                       help="有失败也返回 0（默认非 0，供 CI 用）")
    # board bridge
    p_run.add_argument("--board-dir", help="同时把截图灌进评审板目录")
    p_run.add_argument("--board-as-after", action="store_true", help="灌进 <board-dir>/after/")
    p_run.add_argument("--board-baseline", help="改前图目录（配合 --board-as-after）")
    p_run.add_argument("--board-script", help="评审板脚本路径（外部工具）")
    p_run.add_argument("--serve", action="store_true", help="跑完顺便起评审板")
    p_run.add_argument("--serve-port", type=int, help="评审板首选端口（默认取配置）")

    # ---- check (offline assertion on an existing image)
    p_check = sub.add_parser("check", help="对已有截图做离线断言（不起模拟器）")
    p_check.add_argument("--image", required=True)
    p_check.add_argument("--expect", default="", help="必须出现的词，逗号分隔")
    p_check.add_argument("--must-not", default="", help="不许出现的词，逗号分隔")
    p_check.add_argument("--json", action="store_true", help="只输出 JSON")

    # ---- board
    p_board = sub.add_parser("board", help="读写评审板批注")
    p_board.add_argument("--read", metavar="DIR", help="读这个板子目录的批注")
    p_board.add_argument("--status", default="open,rejected,fixed",
                         help="只看这些状态（逗号分隔；传 all 看全部）")
    p_board.add_argument("--import", dest="import_src", metavar="DIR",
                         help="把另一个板子的图 + 人工批注并入目标板")
    p_board.add_argument("--import-url", metavar="URL",
                         help="从正在运行的板子导入（比磁盘新：带上浏览器里未保存的批注）")
    p_board.add_argument("--into", metavar="DIR", help="目标板目录（配合 --import / --import-url）")
    p_board.add_argument("--prefer", choices=["skip", "src"], default="skip",
                         help="同一张图上同名批注冲突时保留谁（默认 skip=保留目标板的）")
    p_board.add_argument("--push", metavar="URL", help="把目标板的批注推给正在运行的板子刷新")
    p_board.add_argument("--set-version", metavar="LABEL",
                         help="记录这批截图来自哪个版本，如 \"0.1.0 (237)\"")
    p_board.add_argument("--side", choices=["before", "after"], default="after",
                         help="配合 --set-version：标记改前还是改后（默认 after）")
    p_board.add_argument("--image", help="配合 --set-version：只标这一张（默认整批）")
    p_board.add_argument("--fixes", metavar="DIR",
                         help="按批注列出「哪个问题修于哪个版本」，看一张图跨版本修复的情况")

    # ---- devices
    p_dev = sub.add_parser("devices", help="列可用模拟器")
    p_dev.add_argument("--all", action="store_true", help="含未启动的")

    # ---- init
    p_init = sub.add_parser("init", help="在当前目录生成 ujshot.toml + 示例流程")
    p_init.add_argument("--name", default="smoke", help="示例流程名")
    p_init.add_argument("--force", action="store_true")

    # ---- doctor
    sub.add_parser("doctor", help="自检：xcrun / axe / OCR / Pillow / 配置")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2

    if args.cmd == "devices":
        try:
            rows = dev.list_devices()
        except dev.ToolError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2
        for d in rows:
            if not args.all and d.state != "Booted":
                continue
            print(d.line())
        return 0

    if args.cmd == "init":
        return _cmd_init(args)

    if args.cmd == "doctor":
        return _cmd_doctor()

    if args.cmd == "check":
        return _cmd_check(args)

    if args.cmd == "board":
        return _cmd_board(args)

    if args.cmd == "run":
        return _cmd_run(args)

    return 2


# --------------------------------------------------------------------------- run

def _cmd_run(args) -> int:
    cfg = config.load(args.dir)
    if args.device:
        cfg.device = args.device
    if args.bundle:
        cfg.bundle_id = args.bundle
    if args.out:
        cfg.out_dir = os.path.abspath(args.out)
    if args.appearance:
        cfg.appearance = args.appearance
    if args.board_script:
        cfg.board_script = args.board_script

    flow_path = config.resolve_flow(cfg, args.flow)
    try:
        fl = flowmod.load(flow_path)
    except flowmod.FlowError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    try:
        udid = dev.resolve_device(cfg.device)
        axe = dev.which_axe()
    except dev.ToolError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    booted = dev.ensure_booted(udid)
    if booted:
        print(f"  已启动模拟器 {cfg.device}")

    if args.dry_run:
        print(f"✓ 流程 {fl.name}（{len(fl.steps)} 步，{len(fl.shot_ids)} 屏）语法与设备均可用")
        print(f"  设备 {cfg.device} ({udid[:8]}) · 配置 {cfg.describe()}")
        return 0

    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = args.out and os.path.abspath(args.out) or os.path.join(cfg.out_dir, f"{stamp}-{fl.name}")
    os.makedirs(outdir, exist_ok=True)

    print(f"设备 {cfg.device} ({udid[:8]}) · 流程 {fl.name} · 输出 {outdir}")
    print(f"  {ocr.version_note()}")

    if args.app:
        try:
            dev.install(udid, args.app)
            print(f"  已安装 {args.app}")
        except dev.ToolError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2

    if args.seed:
        try:
            _seed(udid, cfg, args.seed, args.seed_to)
        except dev.ToolError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2

    dev.prepare(udid, cfg.appearance)

    rn = runner.Runner(axe=axe, udid=udid, flow=fl, outdir=outdir,
                       bundle=cfg.bundle_id, wait_scale=args.wait_scale,
                       danger_words=cfg.danger_words or None)
    if args.no_restart:
        fl.restart = False
    try:
        result = rn.run()
    except dev.ToolError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    result.device = _device_row(udid, cfg.device)
    # 版本以「装上去那份」的 Info.plist 为准：仓库里的数字和模拟器上的包
    # 完全可能不是一回事（本机改了但没出包），证据上写的必须是后者。
    version = dev.app_version(udid, cfg.bundle_id, args.app or "")
    if version["label"]:
        print(f"  版本 {version['label']}")
    else:
        print("  版本 未知（读不到 Info.plist，图册里会标「未标版本」）")

    meta = {
        "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "seed": os.path.basename(args.seed) if args.seed else "",
        "app": os.path.basename(args.app.rstrip("/")) if args.app else "",
        "version": version["label"],
        "bundle": cfg.bundle_id,
    }
    page = gallery.write(result, flow_note=fl.note, extra_meta={k: v for k, v in meta.items() if v})

    shots = len(result.shots)
    print(f"\n完成：{shots} 屏（异常 {len(result.anomalies)}），失败步骤 {result.failures}")
    print(f"图册：{page}")

    if args.board_dir:
        bdir = board.prepare(os.path.abspath(args.board_dir))
        placed, seeded = board.copy_shots(
            bdir, outdir, result.shots, as_after=args.board_as_after,
            baseline_dir=args.board_baseline, before_map=fl.before_map)
        count = board.write_annotations(bdir, result.shots, as_after=args.board_as_after,
                                        when=meta["when"])
        # 版本跟着图一起进板子：不然过两天就没人知道这批是哪个 Build
        side = "after" if args.board_as_after else "before"
        if version["label"]:
            board.write_versions(bdir, side, version["label"])
            print(f"  版本 → {side}: {version['label']}（写进 versions.json）")
        print(f"评审板：{bdir}")
        print(f"  放下 {len(placed)} 张 → {'after/' if args.board_as_after else '根目录'}")
        if seeded:
            print(f"  基线图 {len(seeded)} 张 ← {args.board_baseline}")
        print(f"  写入 {count} 条自动批注（source=ujshot，重跑覆盖上一轮）")
        port = args.serve_port or cfg.board_port
        if args.serve:
            try:
                url = board.serve(bdir, cfg.board_script, port=port, open_browser=True, udid=udid)
                print(f"  板子：{url}")
            except RuntimeError as exc:
                print(f"  ! {exc}", file=sys.stderr)
        else:
            url = board.known_url(bdir)
            if url and board.push_annotations(url, bdir):
                print(f"  已刷新已开着的板子：{url}")

    if args.open:
        subprocess.run(["open", page], check=False)

    if result.ok or args.keep_going:
        return 0
    return 1


def _device_row(udid: str, name: str) -> Device:
    try:
        for d in dev.list_devices():
            if d.udid == udid:
                return d
    except dev.ToolError:
        pass
    return Device(name=name, udid=udid, state="Booted")


def _seed(udid, cfg, path, override_target) -> None:
    if not os.path.exists(path):
        raise dev.ToolError(f"种子文件不存在：{path}")
    rel = override_target or cfg.state_path
    if not rel:
        raise dev.ToolError("没有指定种子写入位置：用 --seed-to 或配置 [app] state_path")
    container = dev.app_container(udid, cfg.bundle_id)
    target = os.path.join(container, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(path, "rb") as src, open(target, "wb") as dst:
        dst.write(src.read())
    print(f"  种子 → {target}")


# --------------------------------------------------------------------------- check

def _cmd_check(args) -> int:
    if not os.path.exists(args.image):
        print(f"✗ 图片不存在：{args.image}", file=sys.stderr)
        return 2
    ocr.ensure(verbose=False)
    info = ocr.recognize(args.image)
    if info is None:
        payload = {"file": args.image, "verdict": "ocr-unavailable",
                   "error": "OCR 不可用（需要 Xcode 命令行工具）"}
        print(json.dumps(payload, ensure_ascii=False) if args.json else f"✗ {payload['error']}")
        return 2
    text = ocr.text_of(info)
    want = [w.strip() for w in args.expect.split(",") if w.strip()]
    forbid = [w.strip() for w in args.must_not.split(",") if w.strip()]
    missing = [w for w in want if w not in text]
    leaked = [w for w in forbid if w in text]
    verdict = "ok" if not missing and not leaked else ("expect-missing" if missing else "unexpected-text")
    payload = {"file": args.image, "verdict": verdict, "missing": missing,
               "leaked": leaked, "text": text[:300]}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0 if verdict == "ok" else 1


# --------------------------------------------------------------------------- board

def _cmd_board(args) -> int:
    if args.fixes:
        try:
            mapping = board.fix_builds(args.fixes, args.image or "")
        except FileNotFoundError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2
        rows = board.read(args.fixes, statuses=None)
        per = {}
        for r in rows:
            per.setdefault(r["image"], []).append(r)
        out = []
        for img in sorted(per):
            issues = [{"id": r["id"], "status": r["status"],
                       "fixedIn": r["fixedIn"] or board.fixed_in(r) or "",
                       "text": (r["text"] or "").splitlines()[0][:60]} for r in per[img]]
            builds = sorted({i["fixedIn"] for i in issues if i["fixedIn"]})
            out.append({"image": img, "builds": builds, "issues": issues,
                        "crossVersion": len(builds) > 1})
        print(json.dumps({"board": args.fixes,
                          "images": len(out),
                          "crossVersion": [o["image"] for o in out if o["crossVersion"]],
                          "detail": out}, ensure_ascii=False, indent=1))
        return 0

    if args.set_version:
        if not args.into:
            print("✗ --set-version 需要 --into <板子目录>", file=sys.stderr)
            return 2
        try:
            data = board.write_versions(args.into, args.side, args.set_version, args.image or "")
        except (ValueError, OSError) as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2
        scope = args.image or "整批"
        print(f"已记录 {args.side} {scope} = {args.set_version}")
        print(json.dumps(data, ensure_ascii=False, indent=1))
        if args.push and board.push_annotations(args.push, args.into):
            print(f"已刷新运行中的板子：{args.push}")
        return 0

    if args.import_src or args.import_url:
        if not args.into:
            print("✗ 导入需要 --into <目标板目录>", file=sys.stderr)
            return 2
        try:
            stat = board.merge(src_dir=args.import_src or "", dst_dir=args.into,
                               src_url=args.import_url or "", prefer=args.prefer)
        except Exception as exc:
            print(f"✗ 导入失败：{exc}", file=sys.stderr)
            return 2
        print(f"来源：{stat['source']}")
        print(f"图片：{stat['images']} 张改前 + {stat['after']} 张改后")
        print(f"批注：新增 {stat['added']}，重命名避让 {stat['renamed']}，"
              f"重复跳过 {stat['duplicate']}")
        print(f"目标：{args.into}")
        if args.push and board.push_annotations(args.push, args.into):
            print(f"已刷新运行中的板子：{args.push}")
        return 0

    if args.push:
        ok = board.push_annotations(args.push, args.into or "")
        print("已刷新" if ok else "✗ 推送失败")
        return 0 if ok else 1

    if not args.read:
        print("✗ 需要 --read <板子目录>，或用 --import/--import-url + --into", file=sys.stderr)
        return 2
    statuses = None if args.status.strip().lower() == "all" else tuple(
        s.strip() for s in args.status.split(",") if s.strip())
    try:
        rows = board.read(args.read, statuses)
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"board": args.read, "count": len(rows), "items": rows},
                     ensure_ascii=False, indent=1))
    return 0


# --------------------------------------------------------------------------- init / doctor

CONFIG_TEMPLATE = """# ujshot config — 放在仓库根目录，ujshot 会从当前目录向上自动查找。
# 只有 [app] 的 bundle_id 是必填，其余都可省。

[app]
bundle_id = "{bundle}"
# 种子数据写进 App 容器的哪个文件（相对 data container）；不用种子就留空
state_path = ""

[device]
name = "iPhone 18 Pro Max"
appearance = "dark"

[paths]
out = "ujshot-out"
flow_dir = "flows"

[board]
# 可选：外部评审板脚本（能读 <dir>/ + <dir>/after/ + annotations.json 的工具）
script = ""
port = 8766

[safety]
# 可选：覆盖默认的「会改数据」文案列表（逗号分隔）。命中即跳过该次 tap。
# danger_words = "删除,清空,退出登录"
"""


def _cmd_init(args) -> int:
    root = os.path.abspath(args.dir)
    cfg_path = os.path.join(root, config.CONFIG_NAME)
    if os.path.exists(cfg_path) and not args.force:
        print(f"✗ {config.CONFIG_NAME} 已存在（加 --force 覆盖）", file=sys.stderr)
        return 2

    bundle = os.environ.get("UJSHOT_BUNDLE", "")
    if not bundle:
        try:  # 顺手从 project.yml / *.xcodeproj 猜一个，猜不到留占位
            for name in ("project.yml", "project.yaml"):
                p = os.path.join(root, name)
                if os.path.exists(p):
                    import re
                    with open(p, encoding="utf-8") as f:
                        m = re.search(r"PRODUCT_BUNDLE_IDENTIFIER:\s*(\S+)", f.read())
                    if m:
                        bundle = m.group(1).strip('"')
                        break
        except OSError:
            pass
    if not bundle:
        bundle = "com.example.app"

    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(CONFIG_TEMPLATE.format(bundle=bundle))
    print(f"✓ 写入 {cfg_path}" + ("（bundle_id 是猜的，请核对）" if bundle == "com.example.app" else ""))

    flow_dir = os.path.join(root, "flows")
    os.makedirs(flow_dir, exist_ok=True)
    example = os.path.join(flow_dir, f"{args.name}.json")
    if not os.path.exists(example) or args.force:
        with open(example, "w", encoding="utf-8") as f:
            json.dump(flowmod.new_flow_template(args.name), f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"✓ 写入示例流程 {example}")
    print("\n下一步：\n  ujshot devices\n  ujshot run --flow " + args.name + " --open")
    return 0


def _cmd_doctor() -> int:
    ok = True

    def line(status: str, name: str, detail: str = "") -> None:
        print(f"  {status}  {name}{('  ' + detail) if detail else ''}")

    print(f"ujshot {__version__}")
    for tool, hint in (("xcrun", "xcode-select --install"),
                       ("swiftc", "装 Xcode 或命令行工具")):
        path = __import__("shutil").which(tool)
        if path:
            line("✓", tool, path)
        else:
            ok = False
            line("✗", tool, f"缺失（{hint}）")
    try:
        axe = dev.which_axe()
        line("✓", "axe", axe)
    except dev.ToolError as exc:
        ok = False
        axe = None
        line("✗", "axe", str(exc).splitlines()[0])

    if ocr.ensure(verbose=False):
        line("✓", "OCR", "Vision helper 已就绪")
    else:
        line("!", "OCR", "不可用：文字断言会报 ocr-unavailable（不会假通过）")

    try:
        import PIL  # noqa: F401
        line("✓", "Pillow", f"像素 diff 可用（{PIL.__version__}）")
    except ImportError:
        line("!", "Pillow", "未安装：no-visible-change 判定跳过（pip install pillow）")

    cfg = config.load(".")
    if cfg.source:
        line("✓", "配置", cfg.source)
    else:
        line("!", "配置", "没有 ujshot.toml（用 ujshot init 生成）")
    line("·", "默认设备", cfg.device)
    line("·", "Bundle", cfg.bundle_id or "（未设置）")

    if axe:
        try:
            rows = dev.list_devices()
            booted = [d for d in rows if d.state == "Booted"]
            line("✓", "模拟器", f"{len(rows)} 台可用，{len(booted)} 台已启动")
            for d in booted[:5]:
                line("·", "", d.name)
        except dev.ToolError as exc:
            line("✗", "模拟器", str(exc))

    print("\n" + ("全部就绪。" if ok else "有缺项，见上面的 ✗。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
