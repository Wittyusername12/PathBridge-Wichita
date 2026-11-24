/* naipc_engine.js \u2014 BUILD B2 (D1): the browser half of the shared engine. It ranks one person's options from the page's bundle, which
   naipc_engine.py writes (for_page): the published table, each record's values as the table reads them (annotations applied), the
   checks that need neither the person nor the clock (closures, exclusions, the record's own words, stale readings) and each record's
   hours already parsed. The gates, factors, tenths, tie-breakers, classes and every trace line here are run_case's (naipc_engine.py),
   word for word: tests/t_parity_b2.py runs this file in Node against Python on the presets and a grid of people and allows no
   difference. No model call; nothing leaves the page.
   Known limits (B2 \u00a79): text is lower-cased with toLowerCase plus the casefold pairs below (Python's casefold covers more scripts);
   digits in age ranges are ASCII (Python's \d also reads other scripts' digits); a float inside a list shows as JS writes it. */
(function (root) {
  "use strict";
  /* BUILD IC-4 (D8): NR.join_clauses' twin - parts joined by "; ", except after a part that ends a sentence: one space and a capital */
  function joinClauses(parts, sep) {
    if (sep === undefined) sep = "; ";
    var out = "";
    parts.forEach(function (p) {
      p = String(p);
      if (!out) { out = p; }
      else if (/[.!?][\u201d"\u2019')\]]*$/.test(out.replace(/(?:\s|<[^>]+>)+$/, ""))) { out = out.replace(/\s+$/, "") + " " + p.charAt(0).toUpperCase() + p.slice(1); }
      else { out += sep + p; }
    });
    return out;
  }
  var DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  var WS = /[\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/;   /* str.split()'s whitespace */
  var FOLD = { "\u00df": "ss", "\u1e9e": "ss", "\u03c2": "\u03c3", "\u017f": "s", "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st" };
  var OWN = Object.prototype.hasOwnProperty;

  function has(o, k) { return o !== null && o !== undefined && typeof k === "string" && OWN.call(o, k); }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function isObj(v) { return v !== null && typeof v === "object" && !Array.isArray(v); }
  function norm(s) {
    return String(s).toLowerCase().replace(/[\u00df\u1e9e\u03c2\u017f\ufb00-\ufb06]/g, function (c) { return FOLD[c] || c; }).split(WS).filter(Boolean).join(" ");
  }
  function vals(v) {
    if (typeof v === "string") return [norm(v)];
    if (Array.isArray(v) && v.every(function (x) { return typeof x === "string"; })) return v.map(norm);
    return null;
  }
  function pyEq(a, b) {                     /* Python ==: True == 1, 1 == 1.0, None for a missing key; lists and dicts by content */
    if (a === undefined) a = null; if (b === undefined) b = null;
    if ((typeof a === "number" || typeof a === "boolean") && (typeof b === "number" || typeof b === "boolean")) return Number(a) === Number(b);
    if (Array.isArray(a) || Array.isArray(b)) {
      if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
      for (var i = 0; i < a.length; i++) if (!pyEq(a[i], b[i])) return false;
      return true;
    }
    if (isObj(a) && isObj(b)) {
      var ka = Object.keys(a), kb = Object.keys(b);
      if (ka.length !== kb.length) return false;
      return ka.every(function (k) { return OWN.call(b, k) && pyEq(a[k], b[k]); });
    }
    return a === b;
  }
  function pyStr(x) {                       /* str(x) for a list element */
    if (x === true) return "True"; if (x === false) return "False"; if (x === null || x === undefined) return "None";
    return String(x);
  }
  function g6(v) {                          /* f"{v:g}": six significant digits, no trailing zeros, exponent below 1e-4 or from 1e6 */
    var s = v.toPrecision(6), m, mant, ex;
    if (v !== 0 && s.indexOf("e") < 0 && Math.abs(v) < 1e-4) s = v.toExponential(5);
    if (s.indexOf("e") >= 0) {
      m = s.split("e"); mant = m[0].indexOf(".") >= 0 ? m[0].replace(/0+$/, "").replace(/\.$/, "") : m[0];
      ex = parseInt(m[1], 10);
      return mant + "e" + (ex < 0 ? "-" : "+") + (Math.abs(ex) < 10 ? "0" : "") + Math.abs(ex);
    }
    return s.indexOf(".") >= 0 ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
  }
  function show(v, isF) {                   /* _show; isF: the value is a Python float (the bundle's "fl"), which JSON cannot carry */
    if (v === true) return "yes"; if (v === false) return "no";
    if (Array.isArray(v)) return v.length ? v.map(pyStr).join(", ") : "none";
    if (isNum(v)) return (Number.isInteger(v) && !isF) ? v.toLocaleString("en-US") : g6(v);
    if (v === null || v === undefined) return "not given";
    return String(v);
  }
  function h(a) { return (a === null || a === undefined ? "None" : String(a)).split("_").join(" "); }
  /* BUILD IC-8 (D12): REG.field_words' twin, word for word (t_parity_b2 and t_defs_ic8 compare them): a data field's name in words */
  var FIELD_ACRONYMS = { id: "ID", ids: "IDs", hud: "HUD", coc: "CoC", zip: "ZIP", er: "ER", sroi: "SROI", kpi: "KPI", va: "VA", ada: "ADA",
    snap: "SNAP", tanf: "TANF", wic: "WIC", lgbtq: "LGBTQ", vash: "VASH", usd: "USD", yoy: "YoY", dcf: "DCF", api: "API", url: "URL",
    pct: "percent", ft: "full-time" };
  function fieldWords(name) {
    var out = [];
    String(name === null || name === undefined || name === "" ? "" : name).split(/[_\s\-]+/).forEach(function (part) {
      (part.match(/[A-Z]{2,}(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|[A-Z]+|\d+[a-z]{1,3}(?![a-z\d])|\d+/g) || []).forEach(function (w) {
        var k = w.toLowerCase();
        out.push(Object.prototype.hasOwnProperty.call(FIELD_ACRONYMS, k) ? FIELD_ACRONYMS[k] : k);
      });
    });
    return out.join(" ");
  }
  function endpointWords(slug) {          /* REG.endpoint_words' twin */
    var t = String(slug === null || slug === undefined ? "" : slug).replace(/^\/+|\/+$/g, ""), i = t.lastIndexOf("/");
    return fieldWords(i >= 0 ? t.slice(i + 1) : t);
  }
  function tenths(x) { return Math.floor(x * 10 + 0.5 + 1e-9); }
  function pts(t) { return Math.floor(t / 10) + "." + (((t % 10) + 10) % 10); }
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function ageBounds(v) {
    if (isNum(v)) return [Math.trunc(v), Math.trunc(v)];
    if (typeof v !== "string") return null;
    var s = norm(v), m;
    if (["all", "any", "all ages", "any age"].indexOf(s) >= 0) return [0, 200];
    m = /^(\d{1,3})\s*\+$/.exec(s) || /^(\d{1,3})\s*(?:and|&)\s*(?:over|older|up)$/.exec(s);
    if (m) return [parseInt(m[1], 10), 200];
    m = /^(\d{1,3})\s*(?:-|\u2013|\u2014|to)\s*(\d{1,3})$/.exec(s);
    if (m && parseInt(m[1], 10) <= parseInt(m[2], 10)) return [parseInt(m[1], 10), parseInt(m[2], 10)];
    m = /^(?:under|below)\s*(\d{1,3})$/.exec(s);
    if (m) return [0, parseInt(m[1], 10) - 1];
    return null;
  }

  function openAt(hv, now) {                /* open_at, from for_page's parsed hours */
    if (!hv || hv.kind === null || hv.kind === undefined) return [null, hv && typeof hv.detail === "string" ? hv.detail : "no hours given"];
    if (hv.kind === "always") return [true, "open 24/7 (\"" + hv.text + "\")"];
    var wd = (now.getDay() + 6) % 7, t = now.getHours() * 60 + now.getMinutes(), prev = (wd + 6) % 7;
    var stamp = DAY_NAMES[wd] + " " + pad(now.getHours()) + ":" + pad(now.getMinutes());
    for (var i = 0; i < hv.rules.length; i++) {
      var days = hv.rules[i][0], a = hv.rules[i][1], b = hv.rules[i][2], inD = days.indexOf(wd) >= 0, inP = days.indexOf(prev) >= 0;
      if ((a < b && inD && a <= t && t < b) || (a >= b && ((inD && t >= a) || (inP && t < b))))
        return [true, "open at " + stamp + " (\"" + hv.text + "\")"];
    }
    return [false, "closed at " + stamp + " (\"" + hv.text + "\")"];
  }

  function rawOf(rec, f) {                  /* B2: a declared "not given" placeholder reads as missing (_ph_view) */
    if (isObj(rec.ph) && has(rec.ph, f) && rec.ph[f].reads_as === "not given") return null;
    return has(rec.raw, f) && rec.raw[f] !== undefined ? rec.raw[f] : null;
  }
  function noCap(rec, f) {                  /* B2: _no_cap's words for a declared "no cap" placeholder, else null */
    var e = isObj(rec.ph) && has(rec.ph, f) ? rec.ph[f] : null;
    return e && e.reads_as === "no cap" ? fieldWords(f) + " " + show(e.v, isFl(rec, f)) + " in the data, read as no cap \u2014 assumed: " + e.why : null;
  }
  function isFl(rec, f) { return Array.isArray(rec.fl) && rec.fl.indexOf(f) >= 0; }
  function valueOf(rec, f) {                /* _value: the annotation's value and quote, else the record's */
    return has(rec.ann, f) ? [rec.ann[f].value, rec.ann[f].quote] : [rawOf(rec, f), null];
  }

  function gate(g, rec, person, now, name) {
    var rule = g.rule;
    if (isObj(g.when)) {
      var wa = g.when.attr;
      if (!has(person, wa)) {                /* B2 (review): applied or not, does it pass? (run_case's rule) */
        var inner = gate(Object.assign({}, g, { when: null }), rec, person, now, name);
        if (inner[0] === "pass") return ["pass", "passes whether or not it applies (" + name + "'s " + h(wa) + " not known): " + inner[1]];
        return ["confirm", "not known for " + name + " (" + h(wa) + "), which decides whether this gate applies"];
      }
      if (!pyEq(person[wa].value, g.when.is)) return ["pass", "does not apply (" + name + "'s " + h(wa) + ": " + show(person[wa].value) + ")"];
    }
    if (rule === "free_above_zero") {
      var nc = noCap(rec, g.fields[0]);
      if (nc !== null) return ["pass", "no cap (" + nc + ")"];
      var fs = g.fields, vs = fs.map(function (f) { return rawOf(rec, f); });
      if (!vs.every(isNum)) return ["confirm", fs.map(fieldWords).join(" or ") + " not a number in the record"];
      var f0 = isFl(rec, fs[0]), f1 = vs.length === 2 && isFl(rec, fs[1]);
      var free = vs[0] - (vs.length === 2 ? vs[1] : 0);
      var txt = show(free, f0 || f1) + " free (" + fieldWords(fs[0]) + " " + show(vs[0], f0) + (vs.length === 2 ? " \u2212 " + fieldWords(fs[1]) + " " + show(vs[1], f1) + ")" : ")");
      return [free > 0 ? "pass" : "fail", txt];
    }
    if (rule === "open_now") {
      var o = openAt(has(rec.hours, g.field) ? rec.hours[g.field] : null, now);
      return [o[0] === null ? "confirm" : (o[0] ? "pass" : "fail"), o[1]];
    }
    if (rule === "bool") {
      var bv = rawOf(rec, g.field);
      if (typeof bv !== "boolean") return ["confirm", fieldWords(g.field) + " not given as yes/no"];
      return [pyEq(bv, g.want) ? "pass" : "fail", fieldWords(g.field) + ": " + show(bv)];
    }
    var vq = valueOf(rec, g.field), v = vq[0], quote = vq[1];
    var vF = !has(rec.ann, g.field) && isFl(rec, g.field);
    var src = quote ? " (the record's own words: \"" + quote + "\")" : "";
    var attr = g.attr;
    if (rule === "in_set") {
      if ((v === null || v === undefined) && g.if_missing === "pass") return ["pass", "no " + fieldWords(g.field) + " limit in the record"];
      var rv = vals(v);
      if (rv === null) return ["confirm", fieldWords(g.field) + " not given in the record"];
      var any = (Array.isArray(g.any_values) ? g.any_values : []).filter(function (x) { return typeof x === "string"; }).map(norm);
      if (rv.some(function (x) { return any.indexOf(x) >= 0; })) return ["pass", fieldWords(g.field) + " " + show(v) + src + ": open to anyone"];
      if (!has(person, attr)) return ["confirm", "not known for " + name + " (" + h(attr) + ") \u2014 " + fieldWords(g.field) + ": " + show(v) + src];
      var pv = vals(person[attr].value) || [];
      return [rv.some(function (x) { return pv.indexOf(x) >= 0; }) ? "pass" : "fail",
              fieldWords(g.field) + " " + show(v) + src + "; " + name + "'s " + h(attr) + ": " + show(person[attr].value)];
    }
    if (rule === "age_range") {
      var b = ageBounds(v);
      if (b === null) return ["confirm", fieldWords(g.field) + " \"" + show(v, vF) + "\" not read as an age range"];
      if (!has(person, attr) || !isNum(person[attr].value)) {
        if (b[0] === 0 && b[1] === 200) return ["pass", fieldWords(g.field) + " " + show(v, vF) + ": every age"];   /* B2 (review): every age is inside */
        return ["confirm", "not known for " + name + " (" + h(attr) + ") \u2014 " + fieldWords(g.field) + ": " + show(v, vF)];
      }
      var age = person[attr].value;
      return [b[0] <= age && age <= b[1] ? "pass" : "fail", fieldWords(g.field) + " " + show(v, vF) + "; " + name + "'s " + h(attr) + ": " + show(age)];
    }
    return ["confirm", "rule not read"];
  }

  function readsText(f) {                   /* reads_text: what a factor's value reads, as the published formula names it */
    if (f.range_top) return "top of " + fieldWords(f.range_top) + " \u2212 the person's " + h(f.minus_attr);
    var fs = (Array.isArray(f.fields) && f.fields.length) ? f.fields : [f.field];
    return fs.length === 2 ? fieldWords(fs[0]) + " \u2212 " + fieldWords(fs[1]) : fieldWords(fs[0]);   /* BUILD IC-8 (D12): in words */
  }

  function factor(f, rec, person) {        /* [term, value, note, value is a float] */
    var form = f.form, val, isF = false;
    if (form === "ratio" && !f.range_top) {
      var nc0 = noCap(rec, (Array.isArray(f.fields) && f.fields.length) ? f.fields[0] : f.field);
      if (nc0 !== null) return [1, "no cap", nc0, false];
    }
    if (form === "bool") {
      var bv = rawOf(rec, f.field);
      if (typeof bv !== "boolean") return [0, bv, "not given as yes/no: 0", isFl(rec, f.field)];
      return [pyEq(bv, f.want) ? 1 : 0, bv, "", false];
    }
    if (form === "match") {
      var v = valueOf(rec, f.field)[0], rv = vals(v), vF = !has(rec.ann, f.field) && isFl(rec, f.field);
      if (!has(person, f.attr)) return [0, v, "the person's " + h(f.attr) + " not known: 0", vF];
      var pv = vals(person[f.attr].value) || [];
      return [rv && rv.some(function (x) { return pv.indexOf(x) >= 0; }) ? 1 : 0, v, "", vF];
    }
    if (f.range_top) {
      var b = ageBounds(rawOf(rec, f.range_top)), a = has(person, f.minus_attr) ? person[f.minus_attr].value : null;
      if (b === null || !isNum(a)) return [0, null, "not computable: 0", false];
      val = Math.min(b[1], 120) - a;
    } else {
      var fs = (Array.isArray(f.fields) && f.fields.length) ? f.fields : [f.field], vs = fs.map(function (x) { return rawOf(rec, x); });
      if (fs.length === 1 && Array.isArray(vs[0])) vs = [vs[0].length];
      else isF = fs.some(function (x) { return isFl(rec, x); });
      if (!vs.every(isNum)) return [0, null, "not in the record: 0", false];
      val = vs[0] - (vs.length === 2 ? vs[1] : 0);
    }
    var ratio = val / f.ref;
    var term = form === "ratio" ? Math.min(1, Math.max(0, ratio)) : Math.min(1, Math.max(0, 1 - ratio));
    return [term, val, "", isF];
  }

  function klass(kind, g, rec) {
    if (kind === "closure" || (g && g.rule === "open_now")) return "closed";
    if (g && g.rule === "free_above_zero" && g.fields.length === 2 && isNum(rawOf(rec, g.fields[0])) && rawOf(rec, g.fields[0]) > 0) return "full";
    return "other";
  }

  function personOf(attrs) {                /* _person: an attribute written as null is unknown */
    var p = Object.create(null);
    if (isObj(attrs)) Object.keys(attrs).forEach(function (a) {
      var s = attrs[a];
      if (isObj(s) && s.value !== null && s.value !== undefined) p[a] = s;
    });
    return p;
  }

  function held(h, r) {                     /* held(): a fail becomes to confirm where the record's own words offer what the gate checks */
    if (r[0] === "fail" && h) return ["confirm", r[1] + "; the record's own words: \"" + h.quote + "\" \u2014 " + h.why];
    return r;
  }
  function runCase(set, attrs, now, name) {
    var spec = set.spec, person = personOf(attrs), ranked = [], excluded = [], confirm = [], fits = 0;
    set.records.forEach(function (rec) {
      var out = rec.pre.map(function (x) { return [x[0], x[1], x[2], x[3], null]; });
      (spec.gates || []).forEach(function (g) {
        var r = held(isObj(rec.holds) && has(rec.holds, g.id) ? rec.holds[g.id] : null, gate(g, rec, person, now, name));
        out.push([g.label || g.rule, r[0], r[1], g.rule, g]);
      });
      var trace = out.map(function (x) { return x[0] + ": " + x[2] + " \u2192 " + x[1]; });
      /* BUILD IC-4 (D3): run_case's fits - the record fits this person whatever its state (no gate of the person's fit fails) */
      if (!out.some(function (x) { return x[1] === "fail" && klass(x[3], x[4], rec) === "other"; })) fits += 1;
      var fails = out.filter(function (x) { return x[1] === "fail"; }), unk = out.filter(function (x) { return x[1] === "confirm"; });
      var row = { id: rec.id, name: rec.name, trace: trace };
      if (rec.check) row.check = rec.check;
      if (fails.length) {
        var f0 = fails[0];
        var ex = Object.assign({}, row, { gate: f0[0], detail: f0[2], klass: klass(f0[3], f0[4], rec), closure: f0[3] === "closure" });
        if (f0[3] === "closure") {            /* BUILD IC-4 (D3): both assertions print - run_case's lines, word for word */
          var said = out.filter(function (x) { return x[3] === "free_above_zero" && x[1] === "pass"; }).map(function (x) { return x[2]; });
          if (said.length) {
            ex.data_says = said[0];
            ex.detail = joinClauses([f0[2], "the " + (spec.endpoint ? endpointWords(spec.endpoint) : spec.id) + " records still read " + said[0]]);   /* IC-4 (D8); IC-8 (D12) */
          }
        }
        excluded.push(ex);
        return;
      }
      if (unk.length) {
        confirm.push(Object.assign({}, row, { gate: unk[0][0], detail: joinClauses(unk.map(function (x) { return x[0] + ": " + x[2]; })) }));   /* IC-4 (D8): one stop */
        return;
      }
      var comps = (spec.factors || []).map(function (f) {
        var r = factor(f, rec, person), t = tenths(f.max * r[0]);
        return { id: f.id, label: f.label || f.id, tenths: t, points: pts(t), value: show(r[1], r[3]), reads: readsText(f), raw: isNum(r[1]) ? r[1] : null,
                 form: f.form, note: r[2] };
      });
      var sc = comps.reduce(function (s, c) { return s + c.tenths; }, 0);
      ranked.push(Object.assign({}, row, { tenths: sc, score: pts(sc), components: comps, _rec: rec }));
    });
    var tb = spec.tie_breakers || [];
    function tie(x, t) {
      var c = x.components.filter(function (c) { return c.id === t; })[0];
      if (c.raw !== null && (c.form === "ratio" || c.form === "inverse")) return c.form === "ratio" ? -c.raw : c.raw;
      return -c.tenths;
    }
    ranked.sort(function (a, b) {
      if (a.tenths !== b.tenths) return b.tenths - a.tenths;
      for (var i = 0; i < tb.length; i++) { var d = tie(a, tb[i]) - tie(b, tb[i]); if (d !== 0) return d < 0 ? -1 : 1; }
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });
    var ref = isObj(spec.referral) ? spec.referral : null, needRef = 0, last = Object.create(null);
    set.records.forEach(function (r) { last[r.id] = r; });          /* Python's by_id: the last record with an id */
    ranked.forEach(function (x) { if (ref && pyEq(rawOf(last[x.id] || x._rec, ref.field), ref.is)) needRef += 1; delete x._rec; });
    var counts = { records: set.records.length, eligible: ranked.length, referral: ref ? needRef : null,
                   full: excluded.filter(function (x) { return x.klass === "full"; }).length,
                   closed: excluded.filter(function (x) { return x.klass === "closed"; }).length,
                   other: excluded.filter(function (x) { return x.klass === "other"; }).length, confirm: confirm.length, fits: fits };
    return { set: spec.id, ranked: ranked, excluded: excluded, confirm: confirm, counts: counts };
  }

  function rankAll(bundle, attrs, now, name) {
    return ((bundle && bundle.sets) || []).map(function (s) { return runCase(s, attrs, now, name || "the person"); });
  }

  var API = { rankAll: rankAll, runCase: runCase, openAt: openAt, ageBounds: ageBounds, show: show, tenths: tenths, pts: pts, norm: norm,
              personOf: personOf, pyEq: pyEq, readsText: readsText, DAY_NAMES: DAY_NAMES,
              fieldWords: fieldWords, endpointWords: endpointWords };      /* BUILD IC-8 (D12): the words' twin, for its test */
  if (typeof module === "object" && module.exports) module.exports = API; else root.NaipcEngine = API;
})(typeof window !== "undefined" ? window : this);
