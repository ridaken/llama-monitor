"""Conservative, log-derived primary layer assignments (not tensor residency)."""
from collections import Counter
import os
import re


_INFO_PREFIX = r"\b(?:print_info|llama_model_print_info|llm_load_print_meta):\s*"
_TOTAL = re.compile(_INFO_PREFIX + r"n_layer\s*=\s*(\d+)\b")
_ALL_TOTAL = re.compile(_INFO_PREFIX + r"n_layer_all\s*=\s*(\d+)\b")
_LAYER = re.compile(r"load_tensors:\s*layer\s+(\d+)\s+assigned to device\s+([^\s,]+)", re.I)
_FILE = re.compile(r"loaded meta data .*? from (.+?) \(version", re.I)
_DEVICE = re.compile(r"using device (\S+) \((.*?)\) \((.*?)\) -", re.I)


def device_identity(value):
    """Normalize UUIDs and PCI bus IDs, including NVML's eight-digit domain."""
    value = (value or "").strip().lower()
    pci = re.fullmatch(r"(?:0{4})?([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d)", value)
    return pci.group(1) if pci else value if value.startswith("gpu-") else None


class LayerPlacement:
    def __init__(self):
        self.blocks = []
        self.current = None
        self.pending_shared = False
        self.fitting = False

    def feed(self, message):
        for line in message.splitlines():
            self._feed_line(line)

    def _feed_line(self, message):
        if re.match(r"^(?:[\d:.]+\s+)?(?:[IWD]\s+)?(?:main:\s*)?build(?:\s*=|:)\s*\d+", message, re.I) or re.search(r"common_params_print_info:\s*build\s+\d+", message):
            self.__init__()
            return
        lower = message.lower()
        if "fitting params to device memory" in lower:
            self.fitting = True
            self.current = None
        if "fitting params to free memory took" in lower:
            self.fitting = False
            self.current = None
            return
        # --fit emits hypothetical model loads before the real allocation.
        if self.fitting:
            return
        shared = bool(re.search(r"\bsplit[_ -]mode\s*[=:]\s*(row|tensor)\b|CUDA_Split.*model buffer", message, re.I))
        file = _FILE.search(message)
        total = _TOTAL.search(message)
        if file or (total and self.current and self.current["total"] is not None):
            self.current = None
        if self.current is None and (file or total or _DEVICE.search(message)):
            self.current = {"path": file.group(1).strip() if file else None,
                            "total": None, "total_all": None, "layers": {}, "devices": {}, "conflict": False,
                            "shared": self.pending_shared}
            self.pending_shared = False
            self.blocks.append(self.current)
        block = self.current
        if block is None:
            self.pending_shared |= shared
            return
        block["shared"] |= shared
        if total:
            block["total"] = int(total.group(1))
        total_all = _ALL_TOTAL.search(message)
        if total_all:
            block["total_all"] = int(total_all.group(1))
        for label, name, identity in _DEVICE.findall(message):
            block["devices"][label.upper()] = {"name": name, "identity": device_identity(identity)}
        for index, label in _LAYER.findall(message):
            index, label = int(index), label.upper()
            previous = block["layers"].get(index)
            block["conflict"] |= previous is not None and previous != label
            block["layers"][index] = label

    def snapshot(self, model_path=None):
        blocks = [b for b in self.blocks if b["total"] is not None]
        if model_path:
            norm = lambda p: os.path.normcase(os.path.normpath(p))
            matches = [b for b in blocks if b["path"] and norm(b["path"]) == norm(model_path)]
            if matches:
                blocks = matches
            elif any(b["path"] for b in blocks):
                return {"status": "unavailable", "reason": "No placement records match the loaded model.", "devices": []}
        # Never combine or guess between main, draft, projector or retry loads.
        # MTP blocks follow the main transformer range. The output follows all
        # repeating blocks, at n_layer_all rather than n_layer on these models.
        complete = []
        for b in blocks:
            output_index = b["total_all"] if b["total_all"] is not None else b["total"]
            if b["conflict"] or b["total"] <= 0 or output_index < b["total"] or len(b["layers"]) < b["total"]:
                continue
            if all(i in b["layers"] for i in range(b["total"])) and all(0 <= i <= output_index for i in b["layers"]):
                complete.append(b)
        if len(blocks) > 1:
            return {"status": "unavailable", "reason": "Cannot identify main-model placement reliably.", "devices": []}
        if not complete or complete[-1] is not blocks[-1]:
            if blocks and blocks[-1]["shared"]:
                return {"status": "shared", "reason": "Weights are shared across devices; exclusive layer counts do not apply.", "devices": []}
            return {"status": "unavailable", "reason": "Startup log lacks complete layer assignments. Restart with debug logging enabled.", "devices": []}
        block = complete[-1]
        total = block["total"]
        output_index = block["total_all"] if block["total_all"] is not None else total
        counts = Counter(block["layers"][i] for i in range(total))
        labels = set(counts) | set(block["devices"]) | {"CPU"}
        if output_index in block["layers"]:
            labels.add(block["layers"][output_index])
        return {"status": "shared" if block["shared"] else "available", "reason": None,
                "total_layers": total, "output_device": block["layers"].get(output_index),
                "devices": [{"label": label, "layers": counts[label],
                             **block["devices"].get(label, {})} for label in sorted(labels)]}


def placement_response(parser, gpu_data, *, online, managed_state=None, model_path=None, config=None):
    if managed_state in ("stopped", "exited"):
        return {"status": "no_model", "reason": "No model loaded", "devices": []}
    if not online:
        return {"status": "loading" if managed_state == "running" else "no_model",
                "reason": "Loading" if managed_state == "running" else "No model loaded", "devices": []}
    result = parser.snapshot(model_path)
    # --tensor-split alone is also used for ordinary layer splitting.
    split_mode = None
    for flag in (config or {}).get("flags", []):
        name, _, inline = flag.get("flag", "").partition("=")
        if flag.get("enabled") is not False and name in ("-sm", "--split-mode"):
            split_mode = inline or flag.get("value")
    if split_mode is None and managed_state:
        split_mode = os.environ.get("LLAMA_ARG_SPLIT_MODE")
    if split_mode in ("row", "tensor"):
        result["status"] = "shared"
        result["reason"] = "Weights are shared across devices; exclusive layer counts do not apply."
    devices = gpu_data.get("devices", [])
    for entry in result["devices"]:
        identity = entry.get("identity")
        matches = [d for d in devices if identity and identity in
                   (device_identity(d.get("uuid")), device_identity(d.get("pci_bus_id")))]
        if len(matches) == 1:
            entry["gpu_index"] = matches[0]["index"]
    return result
