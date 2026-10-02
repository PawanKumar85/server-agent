"""2D Heatmap generation using Seaborn and Matplotlib for node downtime and failure frequency."""

import base64
import io
import os
from pathlib import Path
from typing import List, Optional

# Ensure Matplotlib uses a writable config and cache dir within workspace or home/tmp
MPL_DIR = Path(os.environ.get("MPLCONFIGDIR", Path(__file__).parent / ".cache" / "matplotlib"))
try:
    MPL_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    MPL_DIR = Path.home() / ".cache" / "matplotlib"
    try:
        MPL_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        MPL_DIR = Path("/tmp") / ".matplotlib"
        MPL_DIR.mkdir(parents=True, exist_ok=True)
os.environ["MPLCONFIGDIR"] = str(MPL_DIR)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
import seaborn as sns


def clean_node_name(domain: str) -> str:
    """Format domain or channel name for clean, readable heatmap axis display."""
    if not domain:
        return "unknown"
    parts = domain.split("/")
    host = parts[0]
    # Remove common suffixes for brevity
    for suffix in [".ottlive.co.in", ".co.in", ".com", ".net", ".org"]:
        if host.endswith(suffix):
            host = host[:-len(suffix)]
            break
    if len(parts) > 1:
        chan = parts[1]
        return f"{host}/{chan[:6]}" if len(chan) > 6 else f"{host}/{chan}"
    return host[:14] + ("…" if len(host) > 14 else "")


def generate_2d_heatmap_png(
    nodes: List[dict],
    theme: str = "dark",
    max_nodes: int = 8,
    dpi: int = 140,
) -> bytes:
    """Generate a 2D Seaborn heatmap representing node downtime and failure frequency."""
    is_dark = theme != "light"
    bg_color = "#16171d" if is_dark else "#f8fafc"
    card_bg = "#1e2029" if is_dark else "#ffffff"
    border_color = "#2c2e38" if is_dark else "#e2e8f0"
    text_color = "#e8e9ee" if is_dark else "#1e293b"
    muted_color = "#9a9fb0" if is_dark else "#64748b"

    # Sort nodes so nodes down too many times appear first
    sorted_nodes = sorted(
        nodes,
        key=lambda n: (
            n.get("failedCount", 0),
            n.get("consecutiveFailures", 0),
            1 if n.get("status") == "DOWN" else 0,
        ),
        reverse=True,
    )
    selected_nodes = sorted_nodes[:max_nodes]
    if not selected_nodes:
        selected_nodes = [{"domain": "none", "failedCount": 0, "consecutiveFailures": 0, "pingCount": 1, "status": "UP"}]

    metrics = ["Total Down", "Consecutive Fails", "Fail Rate %", "Downtime Score"]
    n_nodes = len(selected_nodes)
    n_metrics = len(metrics)

    heat_matrix = np.zeros((n_nodes, n_metrics))
    annot_matrix = []

    # Curated thermal palette: cool slate -> vibrant cyan -> emerald -> amber -> vivid orange -> crimson
    thermal_hex = [
        "#1b2333" if is_dark else "#e2e8f0",  # 0 cool baseline
        "#0284c7",  # low blue
        "#059669",  # green
        "#eab308",  # warning amber
        "#f97316",  # orange
        "#ef4444",  # critical red
        "#991b1b",  # severe crimson
    ]
    thermal_cmap = LinearSegmentedColormap.from_list("seaborn_2d_thermal", thermal_hex, N=256)

    max_failed = max([n.get("failedCount", 0) for n in selected_nodes] + [1])

    node_labels = []
    ytick_colors = []
    for i, n in enumerate(selected_nodes):
        fc = n.get("failedCount", 0)
        cf = n.get("consecutiveFailures", 0)
        pc = max(1, n.get("pingCount", 1))
        rate = (fc / pc) * 100
        status = n.get("status", "UP")

        # Composite severity score: 0 to 100
        score = min(100.0, (fc / max_failed) * 50.0 + (cf / max(1, max_failed)) * 30.0 + (rate / 100.0) * 20.0)

        # Normalized values (0.0 to 1.0)
        heat_matrix[i, 0] = min(1.0, fc / max_failed) if max_failed else 0
        heat_matrix[i, 1] = min(1.0, cf / max(1, max_failed))
        heat_matrix[i, 2] = min(1.0, rate / 100.0)
        heat_matrix[i, 3] = score / 100.0

        # Annotations text
        annot_matrix.append([
            f"{fc}",
            f"{cf}",
            f"{rate:.1f}%",
            f"{int(score)}/100",
        ])

        name = clean_node_name(n.get("domain") or n.get("id") or "")
        status_tag = "● DOWN" if status == "DOWN" or cf > 0 else "○ UP"
        node_labels.append(f"{name}  [{status_tag}]")
        ytick_colors.append("#f87171" if status == "DOWN" or cf > 0 else text_color)

    # Configure Seaborn theme
    sns.set_theme(
        style="dark" if is_dark else "white",
        rc={
            "figure.facecolor": bg_color,
            "axes.facecolor": card_bg,
            "text.color": text_color,
            "xtick.color": muted_color,
            "ytick.color": muted_color,
        },
    )

    fig_h = max(3.4, 0.45 * n_nodes + 1.2)
    fig, ax = plt.subplots(figsize=(7.2, fig_h), dpi=dpi)
    fig.patch.set_facecolor(bg_color)
    ax.set_facecolor(card_bg)

    sns.heatmap(
        heat_matrix,
        annot=np.array(annot_matrix),
        fmt="",
        cmap=thermal_cmap,
        vmin=0.0,
        vmax=1.0,
        linewidths=1.8,
        linecolor=border_color,
        cbar_kws={
            "label": "Thermal Severity",
            "shrink": 0.85,
            "aspect": 16,
            "pad": 0.04,
        },
        xticklabels=metrics,
        yticklabels=node_labels,
        ax=ax,
        annot_kws={"fontsize": 8.5, "weight": "bold"},
    )

    ax.set_xticklabels(metrics, fontsize=8.5, color=text_color, weight="bold")
    ax.tick_params(top=True, bottom=False, labeltop=True, labelbottom=False, length=0)

    # Style y-tick labels with status-specific colors
    for tick, color in zip(ax.get_yticklabels(), ytick_colors):
        tick.set_color(color)
        tick.set_fontsize(8)

    # Colorbar styling
    cbar = ax.collections[0].colorbar
    cbar.set_label("Thermal Severity (Cool → Critical)", fontsize=8, color=muted_color, labelpad=6)
    cbar.ax.tick_params(colors=muted_color, labelsize=7)
    cbar.set_ticks([0.0, 0.25, 0.5, 0.75, 1.0])
    cbar.set_ticklabels(["Zero", "Low", "Warn", "High", "Critical"])

    down_count = sum(1 for n in selected_nodes if n.get("failedCount", 0) > 0)
    subtitle = (
        f"{down_count} node{'s' if down_count != 1 else ''} with downtime history • Live failure frequency"
        if down_count > 0
        else "All nodes operational (0 downtime)"
    )

    plt.suptitle("Node Failure Frequency (Heatmap)", color=text_color, fontsize=10.5, weight="bold", y=0.98)
    ax.set_title(subtitle, color=muted_color, fontsize=8, pad=14)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor=fig.get_facecolor(), transparent=False)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# Main entry point and aliases for backwards compatibility
generate_heatmap_png = generate_2d_heatmap_png
generate_3d_heatmap_png = generate_2d_heatmap_png


def generate_2d_heatmap_base64(nodes: List[dict], theme: str = "dark") -> str:
    """Return data URI string for base64 encoded PNG."""
    png_bytes = generate_2d_heatmap_png(nodes, theme=theme)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


generate_heatmap_base64 = generate_2d_heatmap_base64
generate_3d_heatmap_base64 = generate_2d_heatmap_base64
