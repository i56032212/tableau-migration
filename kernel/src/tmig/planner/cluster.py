"""叢集（Tableau 的「分析 → 叢集」）：k-means。

與 Tableau 相同的做法（「Find Clusters in Data」的 How clustering works）：
- 每個變數以最小–最大正規化到 0–1：(x − min) / (max − min)
- 初始中心以分割法（Howard-Harris）：從一個叢集開始，把組內平方和最大的叢集依變異數最大的變數在它的
  平均值切成兩半，以兩半的中心與其他叢集的中心開始 Lloyd 演算法（平方歐氏距離），直到沒有標記換叢集；
  重複到 k 個叢集
- 叢集數自動時以 Calinski-Harabasz 指標取第一個局部最大值（2 到 25 個叢集）
- 量值是 Null 的標記不分叢集（Not Clustered）

Tableau 的叢集編號規則沒有公開：依分割產生的順序編號（叢集 1 是最先的一半）。
"""

from __future__ import annotations

from typing import Any

import numpy as np

MAX_AUTO = 25


def scale(points: np.ndarray) -> np.ndarray:
    """最小–最大正規化；值都相同的變數為 0。"""
    low = points.min(axis=0)
    span = points.max(axis=0) - low
    span[span == 0] = 1.0
    return (points - low) / span


def _lloyd(
    points: np.ndarray, centers: np.ndarray, labels: np.ndarray, steps: int = 300
) -> tuple[np.ndarray, np.ndarray]:
    for _ in range(steps):
        distances = ((points[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new = distances.argmin(axis=1)
        if (new == labels).all():
            break
        labels = new
        for c in range(len(centers)):
            members = points[labels == c]
            if len(members):
                centers[c] = members.mean(axis=0)
    return labels, centers


def kmeans(points: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """分割法的初始中心加上 Lloyd 演算法：回傳每個點的叢集（0 起算）與中心。"""
    n = len(points)
    labels = np.zeros(n, dtype=int)
    centers = points.mean(axis=0, keepdims=True).copy()
    for count in range(1, k):
        within = [((points[labels == c] - centers[c]) ** 2).sum() for c in range(count)]
        target = int(np.argmax(within))
        members = np.flatnonzero(labels == target)
        if len(members) < 2:
            break
        variance = points[members].var(axis=0)
        variable = int(np.argmax(variance))
        mean = points[members, variable].mean()
        upper = members[points[members, variable] > mean]
        lower = members[points[members, variable] <= mean]
        if not len(upper) or not len(lower):
            break
        labels = labels.copy()
        labels[upper] = count
        centers = np.vstack(
            [centers[:target], points[lower].mean(axis=0, keepdims=True), centers[target + 1 :]]
        )
        centers = np.vstack([centers, points[upper].mean(axis=0, keepdims=True)])
        labels, centers = _lloyd(points, centers.copy(), labels)
    return labels, centers


def calinski_harabasz(points: np.ndarray, labels: np.ndarray) -> float:
    n = len(points)
    groups = sorted(set(int(v) for v in labels))
    k = len(groups)
    if k < 2 or n <= k:
        return float("nan")
    mean = points.mean(axis=0)
    between = 0.0
    within = 0.0
    for g in groups:
        members = points[labels == g]
        center = members.mean(axis=0)
        between += len(members) * float(((center - mean) ** 2).sum())
        within += float(((members - center) ** 2).sum())
    if within == 0:
        return float("inf")
    return (between / (k - 1)) / (within / (n - k))


def describe(values: list[list[float]], labels: list[int]) -> dict[str, Any]:
    """叢集的摘要（Tableau 的「描述叢集」）：叢集數、點數、組間／組內／總平方和（正規化後的值），
    各叢集的項目數與各變數的中心（原本的單位），以及各變數的變異數分析（F 統計量與 P 值）。"""
    from tmig.render.trends import f_sf

    raw = np.asarray(values, dtype=float)
    assigned = np.asarray(labels, dtype=int)
    n = len(raw)
    if n == 0:
        return {"count": 0, "points": 0, "clusters": [], "variables": []}
    points = scale(raw)
    groups = sorted(set(int(v) for v in assigned))
    k = len(groups)
    mean = points.mean(axis=0)
    between = np.zeros(points.shape[1])
    within = np.zeros(points.shape[1])
    clusters: list[dict[str, Any]] = []
    for g in groups:
        chosen = assigned == g
        members = points[chosen]
        center = members.mean(axis=0)
        between += len(members) * (center - mean) ** 2
        within += ((members - center) ** 2).sum(axis=0)
        clusters.append(
            {
                "cluster": g,
                "items": len(members),
                "centers": [float(v) for v in raw[chosen].mean(axis=0)],
            }
        )
    variables: list[dict[str, Any]] = []
    for j in range(points.shape[1]):
        f: float | None = None
        p: float | None = None
        if k > 1 and n > k and within[j] > 0:
            f = float((between[j] / (k - 1)) / (within[j] / (n - k)))
            p = f_sf(f, k - 1, n - k)
        variables.append({"f": f, "p": p, "between": float(between[j]), "within": float(within[j])})
    return {
        "count": k,
        "points": n,
        "between": float(between.sum()),
        "within": float(within.sum()),
        "total": float(between.sum() + within.sum()),
        "clusters": clusters,
        "variables": variables,
    }


def cluster(values: list[list[float]], k: int | None = None) -> list[int]:
    """各標記的叢集編號（1 起算）；k 為 None 時自動決定叢集數。"""
    points = scale(np.asarray(values, dtype=float))
    n = len(points)
    if n == 0:
        return []
    if k is not None:
        labels, _ = kmeans(points, max(1, min(k, n)))
        return [int(v) + 1 for v in labels]
    if n < 3:
        return [1] * n
    best = None
    previous = -np.inf
    for count in range(2, min(MAX_AUTO, n - 1) + 1):
        labels, _ = kmeans(points, count)
        score = calinski_harabasz(points, labels)
        if not np.isfinite(score):
            best = labels if best is None else best
            break
        if score < previous:
            break  # 第一個局部最大值是前一個叢集數
        best, previous = labels, score
    if best is None:
        best = np.zeros(n, dtype=int)
    return [int(v) + 1 for v in best]
