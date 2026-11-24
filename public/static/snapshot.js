// Only on the static snapshot (snapshot.py): make search and the station picker work without the server,
// and say plainly that the what-if form needs the live site.
(() => {
  const S = window.SNAPSHOT;
  const trainFor = q => {
    q = q.trim().toLowerCase();
    const num = q.split(/\s+/)[0];
    if (S.trains[num]) return num;
    return Object.keys(S.trains).find(no => (no + " " + S.trains[no]).toLowerCase().includes(q));
  };
  const say = (form, text) => {
    let p = form.querySelector(".snap-msg");
    if (!p) { p = document.createElement("p"); p.className = "form-error snap-msg"; form.append(p); }
    p.textContent = text;
  };

  const search = document.querySelector("form#search");
  if (search) search.addEventListener("submit", e => {
    e.preventDefault();
    const no = trainFor(search.q.value);
    if (no) location.href = "/train/" + no;
    else say(search, "That train is not in this snapshot. The live site can open any of ~4,700 trains.");
  });

  const pick = document.querySelector("form.station-pick");
  if (pick) pick.addEventListener("submit", e => {
    e.preventDefault();
    const code = pick.code.value.trim().toUpperCase();
    if (S.stations.includes(code)) location.href = "/station/" + code;
    else say(pick, "This snapshot has boards for the " + S.stations.length + " busiest stations: " +
                   S.stations.slice(0, 12).join(", ") + " and more.");
  });

  // What-if: the model's answers were worked out in advance (public/whatif/<train>.json) for 0 to 240 min late;
  // a delay in between is interpolated from the two nearest.
  const whatIf = document.querySelector("form.where");
  const no = (location.pathname.match(/\/train\/(\w+)/) || [])[1];
  if (whatIf && no) {
    let data = null;
    const out = document.createElement("div");
    out.className = "panel whatif-out";
    whatIf.after(out);
    const hhmm = (sch, add, day) => {
      const m = Math.round(+sch.slice(0, 2) * 60 + +sch.slice(3) + add);
      const d = day + Math.floor(m / 1440), t = ((m % 1440) + 1440) % 1440;
      return String(Math.floor(t / 60)).padStart(2, "0") + ":" + String(t % 60).padStart(2, "0") + (d ? " day " + (d + 1) : "");
    };
    const late = m => (m < 0 ? "-" : "+") + (Math.abs(m) >= 60 ? Math.floor(Math.abs(m) / 60) + " h " + String(Math.round(Math.abs(m)) % 60).padStart(2, "0") + " min" : Math.round(Math.abs(m)) + " min");
    const run = () => {
      const at = whatIf.at.value, delay = Math.max(0, Math.min(240, +whatIf.delay.value || 0));
      const ds = data.delays, g = data.grid[at];
      if (!g) { out.textContent = "No prediction from this station."; return; }
      let hi = ds.findIndex(d => d >= delay); if (hi <= 0) hi = Math.max(hi, 1);
      const a = ds[hi - 1], b = ds[hi], w = b === a ? 0 : (delay - a) / (b - a);
      const mix = (x, y) => x == null || y == null ? null : x + (y - x) * w;
      const rows = g[a].map((r, i) => [r[0], mix(r[1], g[b][i][1]), mix(r[2], g[b][i][2]), mix(r[3], g[b][i][3])]);
      out.innerHTML = "<h3>What if: " + data.names[data.stops.find(s => String(s[0]) === at)[1]] + ", " + late(delay) +
        "</h3><ul class='whatif-list'>" + rows.map(([i, p50, p10, p90]) => {
          const [, code, sch, day] = data.stops[i];
          return "<li><b>" + data.names[code] + "</b><span class='" + (p50 > 5 ? "loss-text" : "gain-text") + "'>" +
            hhmm(sch, p50, day) + " (" + late(p50) + ")</span><small>" +
            (p10 == null ? "" : "likely " + hhmm(sch, p10, day) + " to " + hhmm(sch, p90, day)) + "</small></li>";
        }).join("") + "</ul><p class='note'>The model's answers, worked out in advance for 0 to 4 hours late." +
        (ds.includes(delay) ? "" : " This delay is interpolated between " + a + " and " + b + " min.") + "</p>";
    };
    whatIf.addEventListener("submit", e => {
      e.preventDefault();
      if (data) return run();
      out.textContent = "Loading the model's answers...";
      fetch("/whatif/" + no + ".json").then(r => r.ok ? r.json() : Promise.reject())
        .then(j => { data = j; run(); })
        .catch(() => { out.textContent = "What-if for this train runs on the live server only."; });
    });
  }
})();
