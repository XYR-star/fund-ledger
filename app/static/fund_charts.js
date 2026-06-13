(() => {
  const ranges = {
    "6m": { months: 6 },
    "1y": { months: 12 },
    "3y": { months: 36 },
    all: {},
  };

  const parseDate = (value) => {
    const [year, month, day] = value.split("-").map(Number);
    return new Date(year, month - 1, day);
  };

  const formatDate = (value) => {
    const d = typeof value === "string" ? parseDate(value) : value;
    const year = String(d.getFullYear()).slice(2);
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  };

  const formatPercent = (value) => `${(value * 100).toFixed(1)}%`;

  const rangeStart = (points, key) => {
    if (!points.length || key === "all") return null;
    const latest = parseDate(points[points.length - 1].date);
    const start = new Date(latest);
    start.setMonth(start.getMonth() - ranges[key].months);
    return start;
  };

  const filteredData = (data, rangeKey) => {
    const start = rangeStart(data.points, rangeKey);
    if (!start) return data;
    const points = data.points.filter((point) => parseDate(point.date) >= start);
    const visiblePoints = points.length ? points : data.points.slice(-1);
    const firstDate = parseDate(visiblePoints[0].date);
    const lastDate = parseDate(visiblePoints[visiblePoints.length - 1].date);
    const markers = data.markers.filter((marker) => {
      const date = parseDate(marker.date);
      return date >= firstDate && date <= lastDate;
    });
    return { points: visiblePoints, markers };
  };

  const xTicks = (points) => {
    if (!points.length) return [];
    if (points.length === 1) return [{ label: formatDate(points[0].date), x: 2 }];
    const indexes = [0, Math.floor((points.length - 1) / 2), points.length - 1];
    return [...new Set(indexes)].map((index) => ({
      label: formatDate(points[index].date),
      x: 2 + (index * 96) / (points.length - 1),
    }));
  };

  const yTicks = (min, max) => {
    if (max === min) return [{ label: formatPercent(max), y: 26 }];
    return [max, (max + min) / 2, min].map((value) => ({
      label: formatPercent(value),
      y: 5 + ((max - value) / (max - min)) * 37,
    }));
  };

  const markerRadius = (marker, markers) => {
    const amounts = markers.map((item) => Number(item.amount || 0)).filter((value) => value > 0);
    if (!amounts.length || !marker.amount) return 0.9;
    const min = Math.min(...amounts);
    const max = Math.max(...amounts);
    if (max === min) return 1.4;
    return 0.8 + ((Number(marker.amount) - min) / (max - min)) * 1.8;
  };

  const render = (wrap, source, rangeKey) => {
    const svg = wrap.querySelector(".fund-overview-chart");
    const yAxis = wrap.querySelector(".chart-axis");
    const xAxis = wrap.querySelector(".chart-xaxis");
    const data = filteredData(source, rangeKey);
    if (!data.points.length) {
      svg.innerHTML = "<text x='2' y='27' fill='currentColor'>暂无净值</text>";
      yAxis.innerHTML = "";
      xAxis.innerHTML = "";
      return;
    }

    const values = data.points.map((point) => point.value).concat(data.markers.map((marker) => marker.value));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const x = (index) => 2 + (data.points.length === 1 ? 0 : (index * 96) / (data.points.length - 1));
    const y = (value) => 5 + ((max - value) / span) * 37;
    const d = data.points.map((point, index) => `${index ? "L" : "M"}${x(index).toFixed(2)},${y(point.value).toFixed(2)}`).join(" ");
    const grid = yTicks(min, max).map((tick) => `<line class="chart-grid-line" x1="2" y1="${tick.y.toFixed(2)}" x2="98" y2="${tick.y.toFixed(2)}"></line>`).join("");
    const markers = data.markers.map((marker) => {
      const index = data.points.findIndex((point) => point.date >= marker.date);
      const safeIndex = Math.max(index, 0);
      const radius = markerRadius(marker, data.markers);
      const title = `${marker.date} ${marker.action === "sell" ? "卖出" : "买入"}${marker.amount ? ` ¥${Number(marker.amount).toFixed(2)}` : ""}`;
      return `<circle cx="${x(safeIndex).toFixed(2)}" cy="${y(marker.value).toFixed(2)}" r="${radius.toFixed(2)}" class="${marker.action === "sell" ? "sell-marker" : "buy-marker"}"><title>${title}</title></circle>`;
    }).join("");

    svg.innerHTML = `${grid}<path d="${d}" class="chart-line fund-line"></path>${markers}`;
    yAxis.innerHTML = yTicks(min, max).map((tick) => `<span style="top:${tick.y.toFixed(2) / 52 * 100}%">${tick.label}</span>`).join("");
    xAxis.innerHTML = xTicks(data.points).map((tick) => `<span style="left:${tick.x.toFixed(2)}%">${tick.label}</span>`).join("");
  };

  document.querySelectorAll(".chart-wrap").forEach((wrap) => {
    const dataNode = wrap.querySelector(".chart-data");
    const svg = wrap.querySelector(".fund-overview-chart");
    if (!dataNode || !svg) return;
    const card = wrap.closest("article, section") || wrap.parentElement;
    const source = JSON.parse(dataNode.dataset.chart);
    const rangeControls = card.querySelector("[data-chart-range]");
    let rangeKey = rangeControls?.querySelector("[data-range='1y']") ? "1y" : "all";

    const activate = () => {
      rangeControls?.querySelectorAll("[data-range]").forEach((button) => {
        button.classList.toggle("active", button.dataset.range === rangeKey);
      });
    };

    rangeControls?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-range]");
      if (!button) return;
      rangeKey = button.dataset.range;
      activate();
      render(wrap, source, rangeKey);
    });

    activate();
    render(wrap, source, rangeKey);
  });
})();
