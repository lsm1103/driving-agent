"""回放适配器：从录制好的 fixture 重放外生信号流（docs/03 §6）。

live 模式一轮 = 1 天；replay 模式一轮 = 几秒。
阶段 0 的所有调参都在 replay 上做完，再上真实时间轴。
"""
import json
from collections import defaultdict
from pathlib import Path


class ReplayWorld:
    def __init__(self, signals_path="fixtures/signals.json",
                 intake_path="fixtures/intake_v2.jsonl"):
        fx = json.loads(Path(signals_path).read_text(encoding="utf-8"))
        self.concern = fx["concern"]
        by_day = defaultdict(list)
        for s in fx["signals"]:
            by_day[s["day"]].append(s)
        self.days = sorted(by_day)
        self.signals = by_day

        # 复用已录制的 Intake 判断 —— 回放不该重复付费
        self.intake = {}
        p = Path(intake_path)
        if p.exists():
            self.intake = {d["id"]: d for d in
                           (json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                            if l.strip())}

    def __len__(self):
        return len(self.days)

    def diff(self, day_index):
        """第 N 天（1-based）的外生变化。"""
        return self.signals[self.days[day_index - 1]]

    def real_date(self, day_index):
        return self.days[day_index - 1]

    def recorded_intake(self, signals):
        """返回 (登记项, 忽略数)。缺录制时返回 None，调用方需实跑 Intake。"""
        if not self.intake:
            return None, 0
        got = [self.intake[s["id"]] for s in signals if s["id"] in self.intake]
        if len(got) < len(signals) * 0.9:      # 录制不完整就别用
            return None, 0
        reg = [d for d in got if d["register"]]
        return reg, len(got) - len(reg)
