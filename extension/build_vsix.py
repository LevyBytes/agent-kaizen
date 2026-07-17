"""Zero-dependency VSIX packager (Python stdlib only; any Python 3.8+).

Replaces @vscode/vsce for LOCAL packaging: vsce pulls a dependency tree containing unmaintained
packages (prebuild-install, whatwg-encoding at time of removal), which violates this project's
no-unmaintained-dependencies posture — and none of that tooling is needed to produce the artifact.
A VSIX is an OPC zip: ``[Content_Types].xml`` + ``extension.vsixmanifest`` at the root and the
extension files under ``extension/``. This script derives the manifest from package.json, packs an
EXPLICIT allowlist (never node_modules/src/tests/.vscode), and writes deterministic entries (fixed
timestamps, forward-slash names) so identical inputs produce a byte-identical .vsix.

Run:  python build_vsix.py   (after `npm run compile`; or use `npm run package` which does both)
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
EPOCH = (1980, 1, 1, 0, 0, 0)  # fixed DOS timestamp: reproducible archives
OUT_JS_FILES = (
    "artifactStore.js", "chatHtml.js", "chatPanel.js", "chatPanelManager.js", "chatRenderer.js",
    "contextStager.js", "conversationController.js", "diffModel.js", "diffProvider.js",
    "eventReducer.js", "extension.js", "followUpQueue.js", "historyModel.js", "imageStager.js",
    "kaizenCli.js", "mentionIndex.js", "panelState.js", "protocol.js", "sessionClient.js",
    "sessionIndex.js", "startDaemon.js", "state.js", "streamReducer.js", "testExtension.js",
    "testExtensionCore.js", "testExtensionTerminal.js", "views.js",
)
WEBVIEW_JS_FILES = ("main.js", "safeMarkdown.js")

# html/css entries are load-bearing: without a matching Content_Type, the webview assets are dropped
# from the OPC package silently (no error) and the chat panel renders blank at runtime — a
# silent-breakage trap. They ship in Wave 3 alongside media/webview/chat.{html,css}.
CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
  <Default Extension="js" ContentType="application/javascript"/>
  <Default Extension="md" ContentType="text/markdown"/>
  <Default Extension="svg" ContentType="image/svg+xml"/>
  <Default Extension="txt" ContentType="text/plain"/>
  <Default Extension="html" ContentType="text/html"/>
  <Default Extension="css" ContentType="text/css"/>
</Types>
"""

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{id}" Version="{version}" Publisher="{publisher}"/>
    <DisplayName>{display}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
    <Categories>{categories}</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.LocalizedLanguages" Value=""/>
    </Properties>
    <License>extension/LICENSE.txt</License>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def main() -> int:
    """Build the allowlisted deterministic VSIX; return 1 for invalid input or missing modules."""
    out_js = [HERE / "out" / "src" / name for name in OUT_JS_FILES]
    # The compiled webview scripts (out/webview/*.js), produced by `tsc -p src/webview` in the compile
    # chain. Wave 2 fills in the real main.js; the placeholder already emits here.
    webview_js = [HERE / "out" / "webview" / name for name in WEBVIEW_JS_FILES]

    # Keep entry-specific diagnostics before the aggregate allowlist check: these are the two runtime
    # entry points users can act on directly, and packaging tests lock the clearer failure contract.
    if not (HERE / "out" / "src" / "extension.js").is_file():
        print("out/src/extension.js is missing — run `npm run compile` first", file=sys.stderr)
        return 1
    if not (HERE / "out" / "webview" / "main.js").is_file():
        print("out/webview/main.js is missing — run `npm run compile` first", file=sys.stderr)
        return 1

    try:
        pkg = json.loads((HERE / "package.json").read_text(encoding="utf-8"))
        name = pkg["name"]
        version = pkg["version"]
        publisher = pkg["publisher"]
        display = pkg["displayName"]
        description = pkg["description"]
        categories = pkg.get("categories", ["Other"])
        engine = pkg["engines"]["vscode"]
        if not all(isinstance(value, str) for value in (name, version, publisher, display, description, engine)):
            raise TypeError("required package fields must be strings")
        if not isinstance(categories, list) or not all(isinstance(value, str) for value in categories):
            raise TypeError("categories must be a string list")
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"invalid package.json: {error}", file=sys.stderr)
        return 1

    missing = [path.relative_to(HERE).as_posix() for path in (*out_js, *webview_js) if not path.is_file()]
    if missing:
        print(f"compiled production module is missing: {missing[0]} — run `npm run compile` first", file=sys.stderr)
        return 1

    manifest = MANIFEST.format(
        id=escape(name, {'"': "&quot;"}),
        version=escape(version, {'"': "&quot;"}),
        publisher=escape(publisher, {'"': "&quot;"}),
        display=escape(display),
        description=escape(description),
        categories=escape(",".join(categories)),
        engine=escape(engine, {'"': "&quot;"}),
    )

    # Explicit allowlist — the packaged set is DECLARED, not discovered, so nothing (node_modules,
    # src, tests, .vscode, maps) can leak in by glob accident.
    files: list[tuple[str, bytes]] = [
        ("[Content_Types].xml", CONTENT_TYPES.encode("utf-8")),
        ("extension.vsixmanifest", manifest.encode("utf-8")),
        ("extension/package.json", (HERE / "package.json").read_bytes()),
        ("extension/README.md", (HERE / "README.md").read_bytes()),
        ("extension/LICENSE.txt", (HERE / "LICENSE").read_bytes()),
        ("extension/media/kaizen.svg", (HERE / "media" / "kaizen.svg").read_bytes()),
        # Chat webview assets (Wave 2 authors these; the build is a Wave-3 step). read_bytes() FAILS
        # FAST if they are absent — that hard error is intentional: a VSIX missing its chat UI must not
        # package silently. Until Wave 2 lands them, `python build_vsix.py` is expected to raise here.
        ("extension/media/webview/chat.html", (HERE / "media" / "webview" / "chat.html").read_bytes()),
        ("extension/media/webview/chat.css", (HERE / "media" / "webview" / "chat.css").read_bytes()),
    ]
    files += [(f"extension/out/src/{p.name}", p.read_bytes()) for p in out_js]
    files += [(f"extension/out/webview/{p.name}", p.read_bytes()) for p in webview_js]

    target = HERE / f"{name}-{version}.vsix"
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for archive_name, data in files:
                info = zipfile.ZipInfo(archive_name, date_time=EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, data)
    except OSError as error:
        print(f"could not write {target.name}: {error}", file=sys.stderr)
        return 1
    print(f"packaged: {target.name} ({len(files)} files, {target.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
