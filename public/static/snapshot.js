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

  const whatIf = document.querySelector("form.where");
  if (whatIf) {
    whatIf.querySelectorAll("select, input, button").forEach(el => el.disabled = true);
    say(whatIf, "What-if (any station, any delay) runs on the live server, not in this snapshot.");
  }
})();
