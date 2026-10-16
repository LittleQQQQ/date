"""交通路網與轉乘路線計算（純程式計算，不碰資料庫）。

路網由「站（Node）」和「有方向的路段（Edge）」組成：
- 同一條路線（line）連續搭乘，不用下車也不用再等車。
- 換線要加上該站的轉乘步行時間，再加上新路線最久要等的時間（班距）。
- 等車一律以最壞情況估，所以算出來的時間是保守的；實際上通常會更早到。

兩種查詢：
- earliest_arrival(A, t, B)：t 從 A 出發，最早幾點到 B（回程用）
- latest_departure(A, B, t)：要在 t 以前到 B，最晚幾點要從 A 出發（去程用）
- excluding(modes)：拿掉某些交通方式的路網，用來找替代走法（例如不搭高鐵）
"""
from __future__ import annotations

import heapq
import itertools
from collections import defaultdict, deque
from dataclasses import dataclass, field

INF = float("inf")


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    transfer_min: int = 3


@dataclass(frozen=True)
class Edge:
    from_id: str
    to_id: str
    mode: str  # 捷運 / 台鐵 / 高鐵 / 輕軌 / 公車
    line: str
    ride_min: int
    headway_min: int
    first: int  # 從 from 站發車的首班（分鐘）
    last: int  # 從 from 站發車的末班（分鐘）
    fare: int | None = None  # 真實車資（TDX）；None 代表由 app/fares.py 估計


@dataclass
class Leg:
    """連續搭同一條路線的一段。"""

    mode: str
    line: str
    from_name: str
    to_name: str
    transfer_min: int  # 這段開始前，在轉乘站走了幾分鐘
    ready: int  # 走到月台、開始等車
    board: int  # 上車（最壞情況）
    alight: int  # 下車
    ride_min: int  # 車程（不含等車）
    last: int  # 上車站這條路線的末班時間
    fare: int | None = None  # 真實車資；同一條路線連續坐好幾段時是各段相加，有一段沒有就是 None（改用估計）

    @property
    def wait(self) -> int:
        return self.board - self.ready

    @property
    def label(self) -> str:
        """顯示用：「捷運板南線」「高鐵」「公車（台中車站—市政府）」。"""
        return self.line if self.line.startswith(self.mode) else f"{self.mode}{self.line}"


@dataclass
class Route:
    depart: int  # 從起點開始等車的時間
    arrive: int  # 抵達終點的時間
    legs: list[Leg] = field(default_factory=list)

    @property
    def label(self) -> str:
        """走法名稱：各段路線依序串起來，例如「高鐵 → 捷運板南線」；名稱一樣就算同一種走法。"""
        return " → ".join(leg.label for leg in self.legs) or "步行"


class TransitGraph:
    def __init__(self, nodes: list[Node], edges: list[Edge]):
        self.nodes = {n.id: n for n in nodes}
        self.out_edges: dict[str, list[Edge]] = defaultdict(list)
        self.in_edges: dict[str, list[Edge]] = defaultdict(list)
        for e in edges:
            if e.from_id in self.nodes and e.to_id in self.nodes:
                self.out_edges[e.from_id].append(e)
                self.in_edges[e.to_id].append(e)
        # 同一次排程會反覆問同樣的問題（同一個場次、同一家餐廳），先記起來
        self._forward: dict[tuple, Route | None] = {}
        self._backward: dict[tuple, Route | None] = {}
        # 拿掉某些交通方式的路網（找替代走法用），同一次排程反覆用，建一次就記起來
        self._variants: dict[frozenset[str], TransitGraph] = {}

    # ---------- 基本工具 ----------

    def excluding(self, modes: frozenset[str]) -> TransitGraph:
        """拿掉某些交通方式的路網；沒有要拿掉的就是自己。"""
        if not modes:
            return self
        if modes not in self._variants:
            edges = [e for out in self.out_edges.values() for e in out if e.mode not in modes]
            self._variants[modes] = TransitGraph(list(self.nodes.values()), edges)
        return self._variants[modes]

    def reachable(self, src: str, dst: str) -> bool:
        """不管時間，路網上有沒有路可以從 src 到 dst。"""
        seen, queue = {src}, deque([src])
        while queue:
            node = queue.popleft()
            if node == dst:
                return True
            for e in self.out_edges[node]:
                if e.to_id not in seen:
                    seen.add(e.to_id)
                    queue.append(e.to_id)
        return False

    @staticmethod
    def _board(e: Edge, ready: int) -> int | None:
        """ready 時開始等 e 這條路線，最壞情況幾點上車；已經沒車回傳 None。"""
        if ready > e.last:
            return None
        if ready < e.first:
            return e.first
        return min(ready + e.headway_min, e.last)

    def simulate(self, t0: int, path: list[Edge]) -> Route | None:
        """沿著固定的路段，從 t0 出發，用最壞情況的等車時間算出每一段的時間。"""
        legs: list[Leg] = []
        t, line = t0, None
        for e in path:
            if e.line == line and legs:  # 同一班車繼續坐
                leg = legs[-1]
                leg.to_name = self.nodes[e.to_id].name
                leg.ride_min += e.ride_min
                leg.alight = t = t + e.ride_min
                leg.fare = None if leg.fare is None or e.fare is None else leg.fare + e.fare
                continue
            transfer = self.nodes[e.from_id].transfer_min if line is not None else 0
            ready = t + transfer
            board = self._board(e, ready)
            if board is None:
                return None
            t = board + e.ride_min
            legs.append(
                Leg(
                    mode=e.mode,
                    line=e.line,
                    from_name=self.nodes[e.from_id].name,
                    to_name=self.nodes[e.to_id].name,
                    transfer_min=transfer,
                    ready=ready,
                    board=board,
                    alight=t,
                    ride_min=e.ride_min,
                    last=e.last,
                    fare=e.fare,
                )
            )
            line = e.line
        return Route(depart=t0, arrive=t, legs=legs)

    # ---------- 最早抵達（回程） ----------

    def earliest_arrival(self, src: str, t0: int, dst: str) -> Route | None:
        key = (src, t0, dst)
        if key not in self._forward:
            self._forward[key] = self._earliest(src, t0, dst)
        return self._forward[key]

    def _earliest(self, src: str, t0: int, dst: str) -> Route | None:
        if src == dst:
            return Route(t0, t0, [])
        # 狀態 (站, 搭哪條路線抵達)：同一站坐不同路線抵達，接下來要不要再等車不一樣
        best: dict[tuple, float] = {(src, None): t0}
        parent: dict[tuple, tuple[tuple, Edge]] = {}
        seq = itertools.count()
        heap = [(t0, next(seq), src, None)]
        while heap:
            t, _, node, line = heapq.heappop(heap)
            if t > best.get((node, line), INF):
                continue
            if node == dst:
                path, state = [], (node, line)
                while state in parent:
                    state, e = parent[state]
                    path.append(e)
                return self.simulate(t0, path[::-1])
            for e in self.out_edges[node]:
                if e.line == line:
                    arrive = t + e.ride_min
                else:
                    ready = t + (self.nodes[node].transfer_min if line is not None else 0)
                    board = self._board(e, ready)
                    if board is None:
                        continue
                    arrive = board + e.ride_min
                state = (e.to_id, e.line)
                if arrive < best.get(state, INF):
                    best[state] = arrive
                    parent[state] = ((node, line), e)
                    heapq.heappush(heap, (arrive, next(seq), e.to_id, e.line))
        return None

    # ---------- 最晚出發（去程） ----------

    def latest_departure(self, src: str, dst: str, deadline: int) -> Route | None:
        key = (src, dst, deadline)
        if key not in self._backward:
            self._backward[key] = self._latest(src, dst, deadline)
        return self._backward[key]

    def _latest(self, src: str, dst: str, deadline: int) -> Route | None:
        if src == dst:
            return Route(deadline, deadline, [])
        # 從終點往回推。狀態 (站, 接下來要搭的路線) 的值 = 那條路線最晚要在幾點從這站開出
        goal = (dst, None)
        best: dict[tuple, float] = {goal: deadline}
        child: dict[tuple, tuple[Edge, tuple]] = {}
        seq = itertools.count()
        heap = [(-deadline, next(seq), dst, None)]
        while heap:
            neg, _, node, line = heapq.heappop(heap)
            dep = -neg
            if dep < best.get((node, line), -INF):
                continue
            for e in self.in_edges[node]:
                if line is None or e.line == line:  # 到終點下車，或同一班車繼續坐
                    bound = dep
                else:  # 在這站換線：要先走轉乘，再等最久一個班距
                    next_edge = child[(node, line)][0]
                    bound = dep - next_edge.headway_min - self.nodes[node].transfer_min
                board = min(bound - e.ride_min, e.last)
                if board < e.first:
                    continue
                state = (e.from_id, e.line)
                if board > best.get(state, -INF):
                    best[state] = board
                    child[state] = (e, (node, line))
                    heapq.heappush(heap, (-board, next(seq), e.from_id, e.line))

        # 起點出發時間 = 第一班車的最晚發車時間 − 最久要等的班距
        options = [
            (dep - child[state][0].headway_min, next(seq), state)
            for state, dep in best.items()
            if state[0] == src and state[1] is not None
        ]
        if not options:
            return None
        leave, _, state = max(options)
        path = []
        while state != goal:
            e, state = child[state]
            path.append(e)
        return self.simulate(leave, path)
