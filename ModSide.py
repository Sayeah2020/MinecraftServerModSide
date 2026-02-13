import os
import re
import json
import toml
import zipfile
import logging
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
from queue import Queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

log = logging.getLogger(__name__)

# helper for locating application directory when frozen by PyInstaller
import sys

def get_base_dir() -> str:
    """Return directory where the application should store data.

    When running as a normal script this is the source file's directory.
    When frozen by PyInstaller, use the directory containing the executable
    instead of the temporary extraction folder.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_record_dir() -> str:
    return os.path.join(get_base_dir(), "record")

@dataclass
class ModInfo:
    """模组信息数据类"""
    modid: str
    name: str
    version: str
    side: Optional[str] = None
    loader: Optional[str] = None
    reasons: Optional[List[str]] = None
    debug: Optional[Dict[str, Any]] = None
    source_files: Optional[List[str]] = None

class ModInfor:
    """
    模组扫描器，遍历目录并提取模组信息
    """
    def __init__(self):
        self.mods: Dict[str, ModInfo] = {}

    def _make_mod_key(self, modid: str, loader: Optional[str], version: Optional[str]) -> str:
        """生成用于字典存储的复合 key：modid_loader_version

        缺失的 loader/version 使用 'unknown' 填充。
        """
        lid = loader or "unknown"
        ver = version or "unknown"
        return f"{modid}_{lid}_{ver}"
    def PathHandler(self, folder_path: str):
        for item in os.listdir(folder_path):
            full_path = os.path.join(folder_path, item)
            if os.path.isdir(full_path):
                self.PathHandler(full_path)
            elif full_path.endswith('.litemod'):
                self.UniversalHandler(full_path)
            elif full_path.endswith('.zip'):
                self.UniversalHandler(full_path)
            elif full_path.endswith('.jar'):
                self.JarFileHandler(full_path)
            else:
                pass

    # 处理函数选择器
    def JarFileHandler(self, folder_path: str):
        with zipfile.ZipFile(folder_path, 'r') as z:
            namelist = set(z.namelist())
            if 'fabric.mod.json' in namelist:
                self.FabricModHandler(folder_path)
            elif 'riftmod.json' in namelist:
                self.RiftModHandler(folder_path)
            elif 'quilt.mod.json' in namelist:
                self.QuiltModHandler(folder_path)
            elif 'mcmod.info' in namelist:
                self.LForgeModHandler(folder_path)
            elif any(info.filename == 'META-INF/mods.toml' for info in z.infolist()):
                self.MForgeModHandler(folder_path)
            elif any(info.filename == 'META-INF/neoforge.mods.toml' for info in z.infolist()):
                self.NeoForgeModHandler(folder_path)
            else:
                self.SpecialHandler(folder_path)

    # 清理JSON数据
    def JSONClean(self, json_string: str):
        json_string = re.sub(r'[\x00-\x1F\x7F]', '', json_string)
        try:
            json_data = json.loads(json_string)
            # 不再 print 全量 JSON，避免刷屏；需要调试时你可以打开
            # formatted_data = json.dumps(json_data, indent=4, ensure_ascii=False)
            # print(f"Formatted JSON:\n{formatted_data}")
            return json_data
        except (TypeError, ValueError, json.JSONDecodeError) as e:
            print(f"格式化 JSON 数据时出错: {e}")
            log.exception("JSON parsing error")
            return None

    # 从 zip 文件中安全读取文本
    def safeRead_from_zip(self, z: zipfile.ZipFile, path: str) -> Optional[str]:
        try:
            with z.open(path) as f:
                return f.read().decode('utf-8', errors='ignore')
        except Exception:
            log.exception(f"Error reading {path} from zip")
            return None

    # 安全加载 JSON，遇到错误时尝试清理后再加载
    def safeLoad_from_json(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(text)
        except Exception:
            cleaned = self.JSONClean(text)
            log.exception("Error loading JSON, attempted cleaning")
            return cleaned if isinstance(cleaned, dict) else None


    def _extract_fabric_side_from_metadata(self, fabric_json: Dict[str, Any]) -> Tuple[str, List[str], Dict[str, Any]]:
        """
        基于 fabric.mod.json 的 environment + entrypoints 给一个“第一结论”
        返回: (side_guess, reasons, debug_info)
        """
        reasons: List[str] = []
        debug: Dict[str, Any] = {}

        env = fabric_json.get("environment", "*")
        debug["environment"] = env

        entrypoints = fabric_json.get("entrypoints", {}) or {}
        debug["entrypoints_keys"] = sorted(list(entrypoints.keys()))

        # environment 直接判定
        if isinstance(env, str) and env.lower() == "client":
            return "client_only", ["fabric.mod.json: environment=client"], debug
        if isinstance(env, str) and env.lower() == "server":
            reasons.append("fabric.mod.json: environment=server")
            # server_only 仍可能有 client entrypoints，但按规范应不会
            return "server_only", reasons, debug

        # entrypoints 判定
        has_main = "main" in entrypoints
        has_server = "server" in entrypoints
        has_client = "client" in entrypoints

        if (has_main or has_server) and not has_client:
            reasons.append("fabric.mod.json: has entrypoints main/server (no client-only entrypoint)")
            return "both", reasons, debug  # 一般是双端可用
        if has_client and not (has_main or has_server):
            reasons.append("fabric.mod.json: only client entrypoint present")
            return "client_only", reasons, debug

        # 无明确结论
        return "unknown", ["fabric.mod.json: no decisive env/entrypoints"], debug

    def _scan_mixins_for_client_sections(self, z: zipfile.ZipFile) -> Tuple[bool, List[str]]:
        """
        扫描 jar 内 mixins*.json，看是否存在 client 段。
        """
        reasons: List[str] = []
        hit = False
        for p in z.namelist():
            # 常见命名：mixins.modid.json / mixins.modid.client.json 等
            if not p.lower().endswith(".json"):
                continue
            base = os.path.basename(p).lower()
            if not base.startswith("mixins"):
                continue
            txt = self.safeRead_from_zip(z, p)
            if not txt:
                continue
            j = self.safeLoad_from_json(txt)
            if not isinstance(j, dict):
                continue

            # 典型结构：{"mixins":[...], "client":[...], "server":[...]}
            if "client" in j and isinstance(j["client"], list) and len(j["client"]) > 0:
                hit = True
                reasons.append(f"mixin config '{p}' contains non-empty 'client' section")
                # 不 break：多收集一点原因
        return hit, reasons

    def _scan_class_bytes_for_client_markers(
        self,
        z: zipfile.ZipFile,
        max_classes: int = 300,
        max_bytes_per_class: int = 200_000
    ) -> Tuple[bool, List[str]]:
        """
        兜底：扫描 class 文件的字节串（常量池里经常包含类名字符串）。
        - 这是不反编译、但足够实用的“强启发式”。
        """
        markers = [
            b"net/minecraft/client/",
            b"net/minecraft/client/MinecraftClient",
            b"com/mojang/blaze3d/",
            b"org/lwjgl/",
            b"net/fabricmc/api/ClientModInitializer",
        ]
        hits: List[str] = []
        scanned = 0

        for p in z.namelist():
            if not p.endswith(".class"):
                continue
            scanned += 1
            if scanned > max_classes:
                break
            try:
                with z.open(p) as f:
                    data = f.read(max_bytes_per_class)
                for m in markers:
                    if m in data:
                        hits.append(f"class bytes contain marker: {m.decode(errors='ignore')}")
                        # 命中一个就够强了，但仍可继续收集少量
                        break
            except Exception:
                log.exception(f"Error reading class file {p} for client marker scan")
                continue

        return (len(hits) > 0), hits

    def _decide_side(
        self,
        loader: str,
        meta_side: str,
        meta_reasons: List[str],
        mixin_client_hit: bool,
        mixin_reasons: List[str],
        class_marker_hit: bool,
        class_reasons: List[str],
    ) -> Tuple[str, List[str]]:
        """
        合并元数据 + mixin + class marker 的最终判定
        """
        reasons = []
        reasons.extend([f"[meta/{loader}] {r}" for r in meta_reasons])

        # 先吃掉最强规则
        if meta_side == "client_only":
            # 元数据明确 client，直接判 client_only
            # 即便没扫描到 class markers，也按规范不允许上服务端
            return "client_only", reasons

        if meta_side == "server_only":
            # 仍然保留风险提示
            if class_marker_hit or mixin_client_hit:
                reasons.extend([f"[mixin] {r}" for r in mixin_reasons])
                reasons.extend([f"[class-scan] {r}" for r in class_reasons])
                reasons.append("WARNING: metadata says server_only but client markers found (possible mispackaged mod)")
                return "risky", reasons
            return "server_only", reasons

        # meta_side == both/unknown
        if class_marker_hit:
            reasons.extend([f"[class-scan] {r}" for r in class_reasons])
            # 这里不一定是 client_only：有些双端 mod 带 client 代码但用 @Environment 隔离
            # 我们给 risky，提示需要实测或进一步解析
            return "risky", reasons

        if mixin_client_hit:
            reasons.extend([f"[mixin] {r}" for r in mixin_reasons])
            # mixin client 段很常见于双端 mod，因此仍判 both（但提示“含客户端逻辑”）
            return "both", reasons

        # 没任何客户端标记
        if meta_side == "both":
            return "both", reasons
        return "unknown", reasons

    #统处理函数
    def UniversalHandler(self, folder_path: str):
        with zipfile.ZipFile(folder_path, 'r') as z:
            if 'mcmod.info' in z.namelist():
                with z.open('mcmod.info') as info_file:
                    raw_data = info_file.read().decode('utf-8')
                    data = self.JSONClean(raw_data)
                    if data is None:
                        return
                    if isinstance(data, list) and len(data) > 0:
                        mod_info = data[0]
                    elif isinstance(data, dict) and "modlist" in data and len(data["modlist"]) > 0:
                        mod_info = data["modlist"][0]
                    else:
                        return
                    modid = mod_info.get('modid', '')
                    if modid:
                        version = mod_info.get('version', '') or 'unknown'
                        loader = 'universal'
                        key = self._make_mod_key(modid, loader, version)
                        if key in self.mods:
                            return
                        self.mods[key] = ModInfo(
                            modid=modid,
                            name=mod_info.get('name', ''),
                            version=version,
                            side='unknown',
                            loader=loader,
                            reasons=['legacy/zip: mcmod.info has no reliable sided metadata'],
                            source_files=[folder_path]
                        )

    # Fabric Mod 处理函数
    def FabricModHandler(self, folder_path: str):
        with zipfile.ZipFile(folder_path, 'r') as z:
            if 'fabric.mod.json' not in z.namelist():
                return

            txt = self.safeRead_from_zip(z, 'fabric.mod.json')
            if not txt:
                return
            data = self.JSONClean(txt)
            if not isinstance(data, dict):
                return

            meta_side, meta_reasons, debug = self._extract_fabric_side_from_metadata(data)
            mixin_hit, mixin_reasons = self._scan_mixins_for_client_sections(z)
            class_hit, class_reasons = self._scan_class_bytes_for_client_markers(z)

            side, reasons = self._decide_side(
                loader="fabric",
                meta_side=meta_side,
                meta_reasons=meta_reasons,
                mixin_client_hit=mixin_hit,
                mixin_reasons=mixin_reasons,
                class_marker_hit=class_hit,
                class_reasons=class_reasons,
            )

            modid = data.get('id', '')
            if modid:
                version = data.get('version', '') or 'unknown'
                loader = 'fabric'
                key = self._make_mod_key(modid, loader, version)
                if key not in self.mods:
                        self.mods[key] = ModInfo(
                            modid=modid,
                            name=data.get('name', ''),
                            version=version,
                            side=side,
                            loader=loader,
                            reasons=reasons,
                            debug=debug,
                            source_files=[folder_path]
                        )

    # Rift Mod（补 side：Rift 没统一标准，主要靠 class scan）
    def RiftModHandler(self, folder_path: str):
        with zipfile.ZipFile(folder_path, 'r') as z:
            if 'riftmod.json' not in z.namelist():
                return
            txt = self.safeRead_from_zip(z, 'riftmod.json')
            if not txt:
                return
            data = self.JSONClean(txt)
            if not isinstance(data, dict):
                return

            # Rift 这里没有环境字段的事实标准：先 unknown
            meta_side = "unknown"
            meta_reasons = ["riftmod.json: no standard sided metadata"]

            mixin_hit, mixin_reasons = self._scan_mixins_for_client_sections(z)
            class_hit, class_reasons = self._scan_class_bytes_for_client_markers(z)

            side, reasons = self._decide_side(
                loader="rift",
                meta_side=meta_side,
                meta_reasons=meta_reasons,
                mixin_client_hit=mixin_hit,
                mixin_reasons=mixin_reasons,
                class_marker_hit=class_hit,
                class_reasons=class_reasons,
            )

            modid = data.get('id', '')
            if modid:
                version = data.get('version', '') or 'unknown'
                loader = 'rift'
                key = self._make_mod_key(modid, loader, version)
                if key not in self.mods:
                        self.mods[key] = ModInfo(
                            modid=modid,
                            name=data.get('name', ''),
                            version=version,
                            side=side,
                            loader=loader,
                            reasons=reasons,
                            source_files=[folder_path]
                        )

    # Quilt Mod（补 side：Quilt 的 environment 字段位置可能不同，先尽量读，再用 class scan）
    def QuiltModHandler(self, folder_path: str):
        with zipfile.ZipFile(folder_path, 'r') as z:
            if 'quilt.mod.json' not in z.namelist():
                return
            txt = self.safeRead_from_zip(z, 'quilt.mod.json')
            if not txt:
                return
            data = self.JSONClean(txt)
            if not isinstance(data, dict):
                return

            loader_data = data.get('quilt_loader', {}) or {}
            meta = loader_data.get('metadata', {}) or {}

            # Quilt 里 environment 可能在不同位置（有的用 "environment"）
            env = loader_data.get("environment", meta.get("environment", "*"))
            meta_side = "unknown"
            meta_reasons = []
            debug = {"environment": env}

            if isinstance(env, str) and env.lower() == "client":
                meta_side = "client_only"
                meta_reasons.append("quilt.mod.json: environment=client")
            elif isinstance(env, str) and env.lower() == "server":
                meta_side = "server_only"
                meta_reasons.append("quilt.mod.json: environment=server")
            else:
                meta_reasons.append("quilt.mod.json: no decisive environment field")

            mixin_hit, mixin_reasons = self._scan_mixins_for_client_sections(z)
            class_hit, class_reasons = self._scan_class_bytes_for_client_markers(z)

            side, reasons = self._decide_side(
                loader="quilt",
                meta_side=meta_side,
                meta_reasons=meta_reasons,
                mixin_client_hit=mixin_hit,
                mixin_reasons=mixin_reasons,
                class_marker_hit=class_hit,
                class_reasons=class_reasons,
            )

            modid = loader_data.get('id', '')
            if modid:
                version = loader_data.get('version', '') or 'unknown'
                loader = 'quilt'
                key = self._make_mod_key(modid, loader, version)
                if key not in self.mods:
                        self.mods[key] = ModInfo(
                            modid=modid,
                            name=meta.get('name', ''),
                            version=version,
                            side=side,
                            loader=loader,
                            reasons=reasons,
                            debug=debug,
                            source_files=[folder_path]
                        )

    # Legacy Forge（mcmod.info 没可靠 side 信息，主要靠 class scan）
    def LForgeModHandler(self, folder_path: str):
        with zipfile.ZipFile(folder_path, 'r') as z:
            if 'mcmod.info' not in z.namelist():
                return

            raw_data = self.safeRead_from_zip(z, 'mcmod.info')
            if not raw_data:
                return
            cleaned_data = self.JSONClean(raw_data)
            if cleaned_data is None:
                return

            if isinstance(cleaned_data, list) and len(cleaned_data) > 0:
                mod_info = cleaned_data[0]
            elif isinstance(cleaned_data, dict):
                mod_info = cleaned_data
            else:
                print("mcmod.info 的数据结构不符合预期")
                return

            meta_side = "unknown"
            meta_reasons = ["forge legacy: mcmod.info has no reliable sided metadata"]
            mixin_hit, mixin_reasons = self._scan_mixins_for_client_sections(z)
            class_hit, class_reasons = self._scan_class_bytes_for_client_markers(z)

            side, reasons = self._decide_side(
                loader="forge_legacy",
                meta_side=meta_side,
                meta_reasons=meta_reasons,
                mixin_client_hit=mixin_hit,
                mixin_reasons=mixin_reasons,
                class_marker_hit=class_hit,
                class_reasons=class_reasons,
            )

            modid = mod_info.get('modid', '')
            if modid:
                version = mod_info.get('version', '') or 'unknown'
                loader = 'forge_legacy'
                key = self._make_mod_key(modid, loader, version)
                if key not in self.mods:
                        self.mods[key] = ModInfo(
                            modid=modid,
                            name=mod_info.get('name', ''),
                            version=version,
                            side=side,
                            loader=loader,
                            reasons=reasons,
                            source_files=[folder_path]
                        )

    # Modern Forge（mods.toml 也基本没有强制 side 字段，主要靠 class scan）
    def MForgeModHandler(self, folder_path: str):
        with zipfile.ZipFile(folder_path, 'r') as z:
            if 'META-INF/mods.toml' not in z.namelist():
                return
            with z.open('META-INF/mods.toml') as toml_file:
                data = toml.loads(toml_file.read().decode('utf-8', errors='ignore'))
                mods = data.get('mods', [])
                if not mods:
                    return
                mod_info = mods[0]
                version = mod_info.get('version', '')
                if version == '${file.jarVersion}':
                    if 'META-INF/MANIFEST.MF' in z.namelist():
                        with z.open('META-INF/MANIFEST.MF') as manifest:
                            for line in manifest:
                                line = line.decode('utf-8', errors='ignore').strip()
                                if line.startswith('Implementation-Version:'):
                                    version = line.split(':', 1)[1].strip()
                                    break

                meta_side = "unknown"
                meta_reasons = ["forge mods.toml: no reliable sided metadata; using class scan"]
                mixin_hit, mixin_reasons = self._scan_mixins_for_client_sections(z)
                class_hit, class_reasons = self._scan_class_bytes_for_client_markers(z)

                side, reasons = self._decide_side(
                    loader="forge",
                    meta_side=meta_side,
                    meta_reasons=meta_reasons,
                    mixin_client_hit=mixin_hit,
                    mixin_reasons=mixin_reasons,
                    class_marker_hit=class_hit,
                    class_reasons=class_reasons,
                )

                modid = mod_info.get('modId', '')
                if modid:
                    loader = 'forge'
                    key = self._make_mod_key(modid, loader, version)
                    if key not in self.mods:
                        self.mods[key] = ModInfo(
                            modid=modid,
                            name=mod_info.get('displayName', ''),
                            version=version,
                            side=side,
                            loader=loader,
                            reasons=reasons,
                            source_files=[folder_path]
                        )

    def NeoForgeModHandler(self, folder_path: str):
        with zipfile.ZipFile(folder_path, 'r') as z:
            if 'META-INF/neoforge.mods.toml' not in z.namelist():
                return
            with z.open('META-INF/neoforge.mods.toml') as toml_file:
                data = toml.loads(toml_file.read().decode('utf-8', errors='ignore'))
                mods = data.get('mods', [])
                if not mods:
                    return
                mod_info = mods[0]

                meta_side = "unknown"
                meta_reasons = ["neoforge mods.toml: no reliable sided metadata; using class scan"]
                mixin_hit, mixin_reasons = self._scan_mixins_for_client_sections(z)
                class_hit, class_reasons = self._scan_class_bytes_for_client_markers(z)

                side, reasons = self._decide_side(
                    loader="neoforge",
                    meta_side=meta_side,
                    meta_reasons=meta_reasons,
                    mixin_client_hit=mixin_hit,
                    mixin_reasons=mixin_reasons,
                    class_marker_hit=class_hit,
                    class_reasons=class_reasons,
                )

                modid = mod_info.get('modId', '')
                if modid:
                    version = mod_info.get('version', '') or 'unknown'
                    loader = 'neoforge'
                    key = self._make_mod_key(modid, loader, version)
                    if key not in self.mods:
                        self.mods[key] = ModInfo(
                            modid=modid,
                            name=mod_info.get('displayName', ''),
                            version=version,
                            side=side,
                            loader=loader,
                            reasons=reasons,
                            source_files=[folder_path]
                        )

    def SpecialHandler(self, folder_path: str):
        # TODO:处理如OptiFine、BetterFPS之类的可执行jar文件
        # 这里也可以做一个 class scan，至少能标 risky/client_only
        try:
            with zipfile.ZipFile(folder_path, 'r') as z:
                meta_side = "unknown"
                meta_reasons = ["special/unknown mod type: no known metadata"]
                mixin_hit, mixin_reasons = self._scan_mixins_for_client_sections(z)
                class_hit, class_reasons = self._scan_class_bytes_for_client_markers(z)

                side, reasons = self._decide_side(
                    loader="special",
                    meta_side=meta_side,
                    meta_reasons=meta_reasons,
                    mixin_client_hit=mixin_hit,
                    mixin_reasons=mixin_reasons,
                    class_marker_hit=class_hit,
                    class_reasons=class_reasons,
                )
                # 这里无法可靠读 modid/name/version，只输出 side 也没意义，暂时 pass
        except Exception:
            log.exception("Error in SpecialHandler")

    # 从保存的 JSON 文件加载模组数据
    def LoadFromJson(self, index: int = 0):
        """
        从保存的 JSON 文件加载模组数据
        
        Args:
            index: 文件索引，0 为最近的文件，数字越大表示时间越长之前的记录
                  例如：LoadFromJson(0) 读取最新文件，LoadFromJson(1) 读取第二新的文件
        
        Returns:
            包含加载的元数据的字典，包括 'current_path' 等信息；如果加载失败则返回 None
        """
        record_dir = get_record_dir()
        
        try:
            # 检查 record 目录是否存在
            if not os.path.exists(record_dir):
                print(f"记录目录不存在: {record_dir}")
                return None
            
            # 获取所有 ModelInfo_*.json 文件
            files = [f for f in os.listdir(record_dir) if f.startswith("ModelInfo_") and f.endswith(".json")]
            
            if not files:
                print("没有找到任何保存的模组记录")
                return None
            
            # 按时间戳排序，最新的在前面（逆序排列）
            files.sort(reverse=True)
            
            # 验证索引
            if index >= len(files):
                print(f"索引超出范围，最多只有 {len(files)} 个文件（索引范围：0-{len(files)-1}）")
                print(f"可用文件列表：")
                for i, f in enumerate(files[:10]):  # 最多显示 10 个
                    print(f"  [{i}] {f}")
                return None
            
            # 加载指定索引的文件
            selected_file = files[index]
            file_path = os.path.join(record_dir, selected_file)
            
            print(f"正在加载: {selected_file}")
            
            # 清空当前数据
            self.mods.clear()
            
            # 读取 JSON 文件
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 处理新格式（包含 metadata 的对象）
            if isinstance(data, dict) and "metadata" in data and "mods" in data:
                metadata = data.get("metadata", {})
                mods_data = data.get("mods", [])
            # 处理旧格式（直接的数组）
            elif isinstance(data, list):
                metadata = {"current_path": ""}
                mods_data = data
            else:
                print("无法识别的 JSON 格式")
                return None
            
            # 将字典转换回 ModInfo 对象
            for mod_dict in mods_data:
                mod_info = ModInfo(
                    modid=mod_dict.get('modid', ''),
                    name=mod_dict.get('name', ''),
                    version=mod_dict.get('version', ''),
                    side=mod_dict.get('side'),
                    loader=mod_dict.get('loader'),
                    reasons=mod_dict.get('reasons'),
                    debug=mod_dict.get('debug'),
                    source_files=mod_dict.get('source_files')
                )
                # 使用复合 key 恢复到 self.mods，保持首次出现的优先级
                key = self._make_mod_key(mod_info.modid, mod_info.loader or 'unknown', mod_info.version or 'unknown')
                if key not in self.mods:
                    self.mods[key] = mod_info
            
            print(f"成功加载 {len(self.mods)} 个模组信息")
            return metadata
        except Exception as e:
            print(f"加载数据时出错：{e}")
            log.exception("Error loading mod data from JSON")
            return None

    # 将收集的模组数据写入 JSON 文件
    def SaveToJson(self, Path: str = "", current_path: str = ""):
        # 如果调用方没有提供路径，则使用记录目录
        if not Path:
            Path = get_record_dir()
        # 使用当前时间戳作为文件名（精确到秒）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ModelInfo_{timestamp}.json"
        filePath = os.path.join(Path, filename)
        
        try:
            # 确保 filePath 所在目录存在
            os.makedirs(os.path.dirname(filePath), exist_ok=True)
            
            # 将 ModInfo 对象转换为字典
            mods_data = []
            for modid, mod_info in self.mods.items():
                mod_dict = asdict(mod_info)
                # 过滤掉 None 值以保持 JSON 清洁
                mod_dict = {k: v for k, v in mod_dict.items() if v is not None}
                mods_data.append(mod_dict)
            
            # 构建包含元数据和模组数据的结构
            save_data = {
                "metadata": {
                    "current_path": current_path,
                    "timestamp": timestamp
                },
                "mods": mods_data
            }
            
            # 写入文件
            with open(filePath, 'w', encoding='utf-8') as file:
                json.dump(save_data, file, indent=4, ensure_ascii=False)
            print(f"已将 {len(mods_data)} 个模组信息导出到 {filePath}")
        except Exception as e:
            print(f"保存数据时出错：{e}")
            log.exception("Error saving mod data to JSON")

class modOperator:
    """
    模组操作器：用于筛选和处理模组信息
    """
    def __init__(self, mod_scanner: 'ModInfor'):
        """
        初始化操作器
        
        Args:
            mod_scanner: ModInfor 实例，包含 self.mods 数据
        """
        self.mod_scanner = mod_scanner
        self.filtered_mods: Dict[str, ModInfo] = {}
    
    def filter_server_and_unknown(self) -> Dict[str, ModInfo]:
        """
        筛选所有 side 标明为 'server_only' 和 'unknown' 的模组
        
        Returns:
            过滤后的模组字典 {modid: ModInfo}
        """
        self.filtered_mods.clear()
        
        for modid, mod_info in self.mod_scanner.mods.items():
            if mod_info.side in ['server_only', 'unknown', 'risky']:
                self.filtered_mods[modid] = mod_info
        
        print(f"筛选完成：找到 {len(self.filtered_mods)} 个服务端/未知模组")
        for modid, mod_info in self.filtered_mods.items():
            print(f"  - {modid}: {mod_info.name} ({mod_info.side})")
        
        return self.filtered_mods
    
    def copy_mods_to_destination(self, source_dir: str, dest_dir: str, modids: Optional[List[str]] = None) -> bool:
        """
        复制指定的模组文件到目标位置
        
        Args:
            source_dir: 原始模组所在目录
            dest_dir: 目标复制位置
            modids: 要复制的 modid 列表，如果为 None 则复制所有已筛选的模组
        
        Returns:
            bool: 操作是否成功
        """
        import shutil
        
        if not self.filtered_mods:
            print("未找到已筛选的模组，请先调用 filter_server_and_unknown()")
            return False
        
        # 确定要复制的模组列表
        mods_to_copy = modids if modids else list(self.filtered_mods.keys())
        
        # 创建目标目录
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as e:
            print(f"创建目标目录失败: {e}")
            log.exception("Error creating destination directory")
            return False
        
        # 复制文件
        success_count = 0
        failed_count = 0
        
        for key in mods_to_copy:
            if key not in self.filtered_mods:
                print(f"⚠ {key} 不在筛选列表中，跳过")
                continue

            mod_info = self.filtered_mods[key]
            copied_any = False
            copied_paths = set()

            # 优先使用记录下的源文件路径（如果有）进行复制
            if getattr(mod_info, 'source_files', None):
                for src in mod_info.source_files:
                    if not src:
                        continue
                    candidate = src if os.path.isabs(src) else os.path.join(source_dir, src)
                    if os.path.isfile(candidate):
                        try:
                            dest_file = os.path.join(dest_dir, os.path.basename(candidate))
                            if candidate in copied_paths:
                                continue
                            shutil.copy2(candidate, dest_file)
                            copied_paths.add(candidate)
                            print(f"✓ 已复制: {os.path.basename(candidate)} (from recorded path)")
                            success_count += 1
                            copied_any = True
                        except Exception as e:
                            print(f"✗ 复制 {candidate} 失败: {e}")
                            failed_count += 1
                            log.exception("Error copying mod file from recorded path")  

            # 如果没有使用记录的路径成功复制，则回退到按 modid 搜索文件
            if not copied_any:
                search_modid = mod_info.modid
                mod_files = self._find_mod_files(source_dir, search_modid)

                if not mod_files:
                    print(f"✗ 无法找到 {search_modid} 的模组文件")
                    failed_count += 1
                    continue

                for mod_file in mod_files:
                    try:
                        if mod_file in copied_paths:
                            continue
                        dest_file = os.path.join(dest_dir, os.path.basename(mod_file))
                        shutil.copy2(mod_file, dest_file)
                        print(f"✓ 已复制: {os.path.basename(mod_file)}")
                        success_count += 1
                    except Exception as e:
                        print(f"✗ 复制 {mod_file} 失败: {e}")
                        failed_count += 1
                        log.exception("Error copying mod file by search")
        
        print(f"复制完成：成功 {success_count} 个，失败 {failed_count} 个")
        return failed_count == 0
    
    def _find_mod_files(self, search_dir: str, modid: str) -> List[str]:
        """
        在指定目录中搜索模组文件
        
        Args:
            search_dir: 搜索目录
            modid: 模组 ID
        
        Returns:
            找到的模组文件路径列表
        """
        found_files = []
        
        if not os.path.isdir(search_dir):
            return found_files
        
        for root, dirs, files in os.walk(search_dir):
            for file in files:
                # 检查文件名是否包含模组 ID（模糊匹配）
                if modid.lower() in file.lower() and file.endswith(('.jar', '.zip', '.litemod')):
                    found_files.append(os.path.join(root, file))
        
        return found_files
    
    def list_filtered_mods(self) -> None:
        """
        列出所有已筛选的模组信息
        """
        if not self.filtered_mods:
            print("未找到已筛选的模组")
            return
        
        print(f"{'=== 已筛选模组列表 ==='}")
        print(f"{'modid':<20} {'name':<30} {'version':<15} {'side':<15} {'loader':<15}")
        print("-" * 95)
        
        for modid, mod_info in self.filtered_mods.items():
            print(f"{modid:<20} {mod_info.name:<30} {mod_info.version:<15} {mod_info.side:<15} {mod_info.loader or 'N/A':<15}")


if __name__ == "__main__":
    class UI:
        def __init__(self, root: tk.Tk):
            self.root = root
            self.root.title("模组同步器 UI")
            self.root.geometry("960x600")
            self.scanner = ModInfor()
            self.operator = modOperator(self.scanner)
            self.selected_path = ""
            self.current_path = ""
            self._ui_queue = Queue()
            self.root.after(50,self._poll_ui_queue)

            self._build_ui()

        def _poll_ui_queue(self):
            while not self._ui_queue.empty():
                try:
                    func = self._ui_queue.get_nowait()
                    func()
                except Exception as e:
                    log.exception(f"Error in UI queue function: {e}")
                    break
            self.root.after(50,self._poll_ui_queue)

        def _build_ui(self):
            # 配置 Treeview 选中项的样式：橘色底色，白色加粗文字
            style = ttk.Style()
            style.map('Treeview',
                background=[('selected', '#FFA500')],  # 橘色背景
                foreground=[('selected', 'white')]     # 白色文字
            )
            # 配置 Treeview 行高，使文字显示更清晰
            style.configure('Treeview', rowheight=24)
            
            pw = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
            pw.pack(fill=tk.BOTH, expand=1)

            # 左区：操作与详情（初始设为三等分）
            left = ttk.Frame(pw)
            pw.add(left)

            # 将三个常用操作按钮并排放置，便于快速点击
            btn_frame = ttk.Frame(left)
            btn_frame.pack(fill=tk.X, padx=6, pady=6)

            btn_select = ttk.Button(btn_frame, text="选择路径(调用GetPath)", command=self._on_select_path)
            btn_select.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0,4))

            btn_ok = ttk.Button(btn_frame, text="确定(开始扫描)", command=self._on_start_scan)
            btn_ok.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=4)

            btn_clear = ttk.Button(btn_frame, text="清空路径与模组数据", command=self._on_clear)
            btn_clear.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(4,0))

            self.path_label = ttk.Label(left, text="当前路径: ")
            self.path_label.pack(fill=tk.X, padx=6, pady=6)

            ttk.Separator(left, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=4)

            lbl = ttk.Label(left, text="选中模组详情:")
            lbl.pack(anchor=tk.W, padx=6)
            self.detail_text = tk.Text(left, height=15)
            self.detail_text.pack(fill=tk.BOTH, expand=1, padx=6, pady=6)

            btn_export_server = ttk.Button(left, text="导出服务端/unknown 模组", command=self._on_export_server)
            btn_export_server.pack(fill=tk.X, padx=6, pady=6)

            # 中区：模组列表（初始设为三等分）
            mid = ttk.Frame(pw)
            pw.add(mid)

            lbl_mid = ttk.Label(mid, text="模组列表 (名称 - side)")
            lbl_mid.pack(anchor=tk.W, padx=6, pady=4)

            # 使用 Treeview 两列：名称 (靠左) 与 side (靠右)
            self.mod_tree = ttk.Treeview(mid, columns=('name','side'), show='headings', selectmode='browse')
            self.mod_tree.heading('name', text='名称', anchor=tk.W)
            self.mod_tree.heading('side', text='side', anchor=tk.E)
            self.mod_tree.column('name', anchor=tk.W, stretch=True, width=220)
            self.mod_tree.column('side', anchor=tk.E, width=100, stretch=False)
            self.mod_tree.pack(fill=tk.BOTH, expand=1, padx=6, pady=6)
            self.mod_tree.bind('<<TreeviewSelect>>', self._on_mod_select)
            # 标记 unknown 为 orange，risky 为 red
            try: 
                self.mod_tree.tag_configure('unknown', foreground='orange')
                self.mod_tree.tag_configure('risky', foreground='red')
            except tk.TclError:
                log.warning("Failed to configure Treeview tags")
                
            btn_export_xlsx = ttk.Button(mid, text="导出当前模组列表为 xlsx", command=self._on_export_xlsx)
            btn_export_xlsx.pack(fill=tk.X, padx=6, pady=6)

            # 右区：JSON 记录（初始设为三等分）
            right = ttk.Frame(pw)
            pw.add(right)

            lbl_right = ttk.Label(right, text="JSON 记录 (点击加载)")
            lbl_right.pack(anchor=tk.W, padx=6, pady=4)

            self.record_listbox = tk.Listbox(right)
            self.record_listbox.pack(fill=tk.BOTH, expand=1, padx=6, pady=6)
            self.record_listbox.bind('<<ListboxSelect>>', self._on_record_select)

            # 将初始 sash 位置设置移到单独的方法以提高可读性
            self._set_initial_panes(pw)

            self._refresh_record_list()
            
        def _set_initial_panes(self, pw: ttk.Panedwindow):
            """
            将 PanedWindow 的 sash 初始位置设置为三等分。
            这个方法与界面组件构建分离，便于阅读与测试。
            """
            try:
                pw.update_idletasks()
                total = pw.winfo_width()
                # 如果宽度尚未就绪，则延迟重试
                if not total or total < 50:
                    self.root.after(100, lambda: self._set_initial_panes(pw))
                    return
                # 将窗口宽度按 2:1:1 分配给 左/中/右 区域
                # 总共 4 份，左区 2 份，其余各 1 份。
                total_parts = 4
                left_w = int(total * 2 / total_parts)
                mid_w = int(total * 1 / total_parts)
                try:
                    pw.sashpos(0, left_w)
                    pw.sashpos(1, left_w + mid_w)
                except Exception:
                    # 不同 Tk 版本可能使用不同方法
                    try:
                        pw.sash_place(0, left_w, 0)
                        pw.sash_place(1, left_w + mid_w, 0)
                    except Exception:
                        pass
            except Exception:
                log.exception("Error setting initial PanedWindow sash positions")

        def _on_select_path(self):
            path = filedialog.askdirectory(title="请选择目标文件夹（将复制服务端/unknown 模组）")
            if not path:
                # 用户点击取消，不做任何处理
                return
            if os.path.isdir(path):
                self.selected_path = path
                self.path_label.config(text=f"当前路径: {path}")
            else:
                # 路径无效，仅弹窗提醒，不强求重新选择
                self._ui_queue.put(lambda: messagebox.showwarning("路径无效", "您选择的文件夹路径无效。"))

        def _on_start_scan(self):
            if not self.selected_path or not os.path.isdir(self.selected_path):
                self._ui_queue.put(lambda: messagebox.showwarning("路径无效", "请先选择有效的模组文件夹路径"))
                return

            def worker():
                self.scanner.mods.clear()
                self.scanner.PathHandler(self.selected_path)
                # 保存记录到 record 目录，同时记录当前路径
                record_dir = get_record_dir()
                self.scanner.SaveToJson(record_dir, self.selected_path)
                self.root.after(0, self._refresh_mod_list)
                self.root.after(0, self._refresh_record_list)

            threading.Thread(target=worker, daemon=True).start()

        def _on_clear(self):
            self.selected_path = ""
            self.path_label.config(text="当前路径: ")
            self.scanner.mods.clear()
            self._refresh_mod_list()

        def _refresh_mod_list(self):
            # 清空 tree
            try:
                children = self.mod_tree.get_children()
                if children:
                    self.mod_tree.delete(*children)
            except Exception:
                log.exception("Error clearing mod tree")
            # 以 modid 排序并插入
            for modid, mod in sorted(self.scanner.mods.items(), key=lambda x: x[0].lower()):
                side_text = mod.side or 'unknown'
                # 根据 side 属性应用不同的标签
                if side_text == 'unknown':
                    tags = ('unknown',)
                elif side_text == 'risky':
                    tags = ('risky',)
                else:
                    tags = ()
                try:
                    # 使用 modid 作为 iid，便于后续查找
                    iid = modid
                    if iid in self.mod_tree.get_children(''):
                        iid = ''  # 或者 f"{modid}__dup"
                    self.mod_tree.insert('', 'end', iid=iid or None, values=(mod.name, side_text), tags=tags)
                except Exception:
                    log.exception(f"Error inserting mod {modid} into treeview")
                    # 回退：不设置 iid
                    self.mod_tree.insert('', tk.END, values=(mod.name, side_text), tags=tags)

        def _on_mod_select(self, event):
            # Treeview 的选中项返回 iid（我们使用 modid 作为 iid）
            try:
                sel = self.mod_tree.selection()
            except Exception:
                sel = ()
                log.exception("Error getting selected mod from treeview")
            if not sel:
                return
            modid = sel[0]
            mod = self.scanner.mods.get(modid)
            if not mod:
                return
            self.detail_text.delete('1.0', tk.END)
            txt = json.dumps(asdict(mod), indent=2, ensure_ascii=False)
            self.detail_text.insert(tk.END, txt)

        def _on_export_server(self):
            # 筛选并复制到目标文件夹（通过对话框选择），选择源优先使用 current_path，其次 selected_path
            if not self.scanner.mods:
                self._ui_queue.put(lambda: messagebox.showinfo("提示", "当前没有模组数据，请先扫描或加载记录"))
                return
            self.operator.filter_server_and_unknown()
            dest = filedialog.askdirectory(title="请选择目标文件夹（将复制服务端/unknown 模组）")
            if not dest:
                return
            # 优先使用 current_path，其次使用 selected_path
            source = self.current_path or self.selected_path
            if not source:
                self._ui_queue.put(lambda: messagebox.showwarning("未指定源", "未指定来源文件夹，无法搜索模组文件。请先扫描或加载记录。"))
                return
            ok = self.operator.copy_mods_to_destination(source, dest)
            if ok:
                # 在 Windows 上打开目标目录
                try:
                    if os.name == 'nt':
                        os.startfile(dest)
                    else:
                        # 通用备用方法
                        import subprocess
                        subprocess.Popen(['explorer', dest])
                except Exception:
                    log.exception("Error opening destination folder")
                self._ui_queue.put(lambda: messagebox.showinfo("完成", "导出完成"))
            else:
                self._ui_queue.put(lambda: messagebox.showwarning("部分失败", "导出过程中存在失败项，详情请查看控制台输出"))

        def _on_export_xlsx(self):
            try:
                import openpyxl
            except ImportError:
                self._ui_queue.put(lambda: messagebox.showerror("缺少依赖", "请安装 openpyxl: pip install openpyxl"))
                return
            if not self.scanner.mods:
                self._ui_queue.put(lambda: messagebox.showinfo("提示", "当前没有模组数据可导出，请选择记录或创建新的解析"))
                return
            f = filedialog.asksaveasfilename(defaultextension='.xlsx', filetypes=[('Excel 文件','*.xlsx')])
            if not f:
                return
            wb = openpyxl.Workbook(write_only=True)
            ws = wb.create_sheet()
            headers = ['modid','name','version','side','loader','reasons','debug']
            ws.append(headers)
            for modid, mod in self.scanner.mods.items():
                row = [mod.modid, mod.name, mod.version, mod.side, mod.loader, json.dumps(mod.reasons, ensure_ascii=False), json.dumps(mod.debug, ensure_ascii=False)]
                ws.append(row)
            wb.save(f)
            self._ui_queue.put(lambda: messagebox.showinfo("已导出", f"已导出到 {f}"))

        def _refresh_record_list(self):
            self.record_listbox.delete(0, tk.END)
            record_dir = get_record_dir()
            if not os.path.isdir(record_dir):
                return
            files = [f for f in os.listdir(record_dir) if f.startswith("ModelInfo_") and f.endswith('.json')]
            files.sort(reverse=True)
            self._record_files = files
            for f in files:
                self.record_listbox.insert(tk.END, f)

        def _on_record_select(self, event):
            sel = self.record_listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            # record 列表是按时间逆序排序，LoadFromJson 接受 index 指定其中的文件
            # 清空现有 mods 并加载
            self.scanner.mods.clear()
            try:
                metadata = self.scanner.LoadFromJson(idx)
                # 从元数据中加载路径
                if metadata and "current_path" in metadata:
                    self.current_path = metadata["current_path"]
                    print(f"已加载记录的路径: {self.current_path}")
                else:
                    self.current_path = ""
            except Exception as e:
                log.exception(f"Error loading record {idx}")
                self._ui_queue.put(lambda: messagebox.showerror("加载失败", str(e)))
                return
            self._refresh_mod_list()

    root = tk.Tk()
    app = UI(root)
    root.mainloop()
