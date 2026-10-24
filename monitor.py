"""Monitor de passagens: GRU → parada nos EUA → Tóquio → GRU, com alerta por e-mail.

Cada execução faz um lote de buscas no Google Flights (via fast-flights), guarda os
preços em SQLite e monta combinações de bilhetes separados para cada cidade de
parada e cada conjunto de datas. Quando uma combinação fica dentro do preço máximo,
os bilhetes dela são buscados de novo e, se o preço se confirmar, sai um e-mail
via Apprise.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import sqlite3
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from urllib.parse import quote

import apprise
from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights
from fast_flights.fetcher import URL
from fast_flights.integrations.base import FetchIntegration
from primp import Client

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "dados" / "precos.db"
MAX_FALHAS_SEGUIDAS = 5
HISTORICO_DIAS = 60
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def br(d: date) -> str:
    return d.strftime("%d/%m")


def reais(v: int) -> str:
    return f"R$ {v:,.0f}".replace(",", ".")


# ---------------------------------------------------------------------------
# Bilhetes e combinações
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    """Uma busca no Google Flights: só ida (1 trecho) ou ida e volta (2 trechos)."""

    legs: tuple[tuple[str, str, date], ...]

    @property
    def key(self) -> str:
        kind = "OW" if len(self.legs) == 1 else "RT"
        return kind + "|" + "|".join(f"{a}-{b}-{d.isoformat()}" for a, b, d in self.legs)

    def label(self) -> str:
        (a, b, d), *rest = self.legs
        if not rest:
            return f"{a}→{b} {br(d)} (só ida)"
        return f"{a}⇄{b} {br(d)}–{br(rest[0][2])} (ida e volta)"

    def query(self, cfg: dict):
        v, b = cfg["viagem"], cfg["busca"]
        return create_query(
            flights=[
                FlightQuery(
                    date=d.isoformat(),
                    from_airport=a,
                    to_airport=z,
                    max_stops=v["max_conexoes_por_trecho"],
                )
                for a, z, d in self.legs
            ],
            trip="one-way" if len(self.legs) == 1 else "round-trip",
            passengers=Passengers(adults=v["adultos"]),
            currency=b["moeda"],
            language=b["idioma"],
            hide_separate_and_self_transfer=b["ocultar_autoconexao"],
        )


def ow(a: str, b: str, d: date) -> Ticket:
    return Ticket(((a, b, d),))


def rt(a: str, b: str, d1: date, d2: date) -> Ticket:
    return Ticket(((a, b, d1), (b, a, d2)))


@dataclass(frozen=True)
class Itinerary:
    hub: str  # cidade da parada nos EUA ("" na referência sem parada)
    saida: date
    ida_japao: date
    volta: date
    estrategia: str
    tickets: tuple[Ticket, ...]

    @property
    def key(self) -> str:
        return self.estrategia + "|" + "+".join(t.key for t in self.tickets)


def build_itineraries(cfg: dict, hubs: list[str]) -> tuple[list[Itinerary], list[Itinerary]]:
    """Gera as combinações com parada e, como referência, a ida e volta sem parada."""
    v = cfg["viagem"]
    o, t = v["origem"], v["destino"]
    saidas = [v["saida_de"] + timedelta(days=i) for i in range((v["saida_ate"] - v["saida_de"]).days + 1)]
    com_parada: list[Itinerary] = []
    referencia: dict[tuple[date, date], Itinerary] = {}
    for hub in hubs:
        for d in saidas:
            for p in v["dias_parada"]:
                # Os voos GRU→EUA chegam no dia seguinte; p dias inteiros na parada
                # significa sair para Tóquio p + 1 dias depois de sair de GRU.
                d2 = d + timedelta(days=p + 1)
                for s in v["dias_no_japao"]:
                    r = d2 + timedelta(days=s)
                    # Volta com bilhete separado passando pela parada: uma noite lá
                    # antes do voo para o Brasil, para não depender de conexão apertada.
                    r1 = r + timedelta(days=1)
                    for nome, tickets in (
                        ("3 bilhetes só de ida", (ow(o, hub, d), ow(hub, t, d2), ow(t, o, r))),
                        ("2 idas e voltas", (rt(o, hub, d, r1), rt(hub, t, d2, r))),
                        ("ida e volta EUA⇄Tóquio + 2 só de ida", (ow(o, hub, d), rt(hub, t, d2, r), ow(hub, o, r1))),
                        ("ida e volta GRU⇄EUA + 2 só de ida", (rt(o, hub, d, r1), ow(hub, t, d2), ow(t, hub, r))),
                        ("4 bilhetes só de ida", (ow(o, hub, d), ow(hub, t, d2), ow(t, hub, r), ow(hub, o, r1))),
                    ):
                        com_parada.append(Itinerary(hub, d, d2, r, nome, tickets))
                    referencia[(d, r)] = Itinerary("", d, d, r, "ida e volta sem parada", (rt(o, t, d, r),))
    return com_parada, list(referencia.values())


# ---------------------------------------------------------------------------
# Banco de preços
# ---------------------------------------------------------------------------


@dataclass
class Quote:
    price: int | None  # None = sem voos ou erro
    details: dict
    fetched_at: datetime


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS quotes (
            key TEXT PRIMARY KEY, price INTEGER, details TEXT, fetched_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history (
            key TEXT NOT NULL, price INTEGER, fetched_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS alerts (
            itinerary TEXT PRIMARY KEY, price INTEGER NOT NULL, sent_at TEXT NOT NULL);
        """
    )
    return db


def load_quotes(db: sqlite3.Connection) -> dict[str, Quote]:
    rows = db.execute("SELECT key, price, details, fetched_at FROM quotes")
    return {k: Quote(p, json.loads(d), datetime.fromisoformat(ts)) for k, p, d, ts in rows}


def prune_history(db: sqlite3.Connection) -> None:
    """Apaga o histórico antigo para o banco não crescer sem limite no cache do GitHub."""
    db.execute("DELETE FROM history WHERE fetched_at < ?", ((now() - timedelta(days=HISTORICO_DIAS)).isoformat(),))
    db.commit()


def save_quote(db: sqlite3.Connection, key: str, price: int | None, details: dict) -> None:
    ts = now().isoformat()
    db.execute(
        "INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?)",
        (key, price, json.dumps(details, ensure_ascii=False), ts),
    )
    db.execute("INSERT INTO history VALUES (?, ?, ?)", (key, price, ts))
    db.commit()


# ---------------------------------------------------------------------------
# Buscas no Google Flights
# ---------------------------------------------------------------------------


class CountryFetcher(FetchIntegration):
    """Mesmo fetch do fast-flights, mas fixando o país de compra (gl) e mantendo os
    cookies entre buscas. Sem o gl, o Google usa o país do IP — nos servidores do
    GitHub, os EUA."""

    def __init__(self, pais: str):
        self.pais = pais
        self.client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)

    def fetch_html(self, q, /) -> str:
        return self.client.get(URL, params={**q.params(), "gl": self.pais}).text


@cache
def fetcher(pais: str) -> CountryFetcher:
    return CountryFetcher(pais)


def fetch(ticket: Ticket, cfg: dict) -> tuple[int | None, dict]:
    """Busca o bilhete e devolve o menor preço e os voos dessa opção (None = sem voos)."""
    try:
        flights = [f for f in get_flights(ticket.query(cfg), integration=fetcher(cfg["busca"]["pais"])) if f.price]
    except FlightsNotFound:
        flights = []
    if not flights:
        return None, {}
    best = min(flights, key=lambda f: f.price)
    voos = [
        f"{x.from_airport.code}→{x.to_airport.code} "
        f"{x.departure.date[2]:02d}/{x.departure.date[1]:02d} {x.departure.time[0]:02d}:{x.departure.time[1]:02d}"
        f" → {x.arrival.date[2]:02d}/{x.arrival.date[1]:02d} {x.arrival.time[0]:02d}:{x.arrival.time[1]:02d}"
        for x in best.flights
    ]
    return best.price, {"cias": best.airlines, "voos": voos}


def pause(cfg: dict) -> None:
    time.sleep(random.uniform(*cfg["busca"]["pausa_segundos"]))


def refresh(db: sqlite3.Connection, cfg: dict, tickets: list[Ticket], limit: int) -> tuple[int, int]:
    """Busca até `limit` bilhetes: primeiro os nunca buscados, depois os mais antigos.

    Devolve (buscas feitas, buscas sem erro).
    """
    quotes = load_quotes(db)
    todo = sorted(tickets, key=lambda t: quotes[t.key].fetched_at if t.key in quotes else EPOCH)[:limit]
    feitas = sucessos = falhas = 0
    for i, t in enumerate(todo, 1):
        if i > 1:
            pause(cfg)
        try:
            price, details = fetch(t, cfg)
            falhas = 0
            sucessos += 1
        except Exception as e:  # mudança no Google, bloqueio, rede...
            price, details = None, {"erro": f"{type(e).__name__}: {e}"}
            falhas += 1
        feitas += 1
        save_quote(db, t.key, price, details)
        resultado = reais(price) if price else details.get("erro", "sem voos")
        log(f"[{i}/{len(todo)}] {t.label()}: {resultado}")
        if falhas >= MAX_FALHAS_SEGUIDAS:
            log("Muitas falhas seguidas (talvez bloqueio do Google); o restante fica para a próxima execução.")
            break
    return feitas, sucessos


# ---------------------------------------------------------------------------
# Avaliação das combinações
# ---------------------------------------------------------------------------


def evaluate(itins: list[Itinerary], quotes: dict[str, Quote], max_age: timedelta) -> list[tuple[int, Itinerary]]:
    """Total de cada combinação cujos bilhetes têm preço recente, do mais barato ao mais caro."""
    limite = now() - max_age
    out = []
    for it in itins:
        qs = [quotes.get(t.key) for t in it.tickets]
        if all(q and q.price and q.fetched_at >= limite for q in qs):
            out.append((sum(q.price for q in qs), it))
    out.sort(key=lambda x: x[0])
    return out


def best_per_trip(ranked: list[tuple[int, Itinerary]]) -> list[tuple[int, Itinerary]]:
    """Mantém só a estratégia mais barata para cada parada + conjunto de datas."""
    seen, out = set(), []
    for total, it in ranked:
        k = (it.hub, it.saida, it.ida_japao, it.volta)
        if k not in seen:
            seen.add(k)
            out.append((total, it))
    return out


def confirm(db: sqlite3.Connection, cfg: dict, candidatos: list[tuple[int, Itinerary]]) -> list[tuple[int, Itinerary]]:
    """Busca de novo os bilhetes das combinações candidatas e recalcula o total."""
    quotes = load_quotes(db)
    recente = now() - timedelta(hours=1)
    falharam = set()
    for t in dict.fromkeys(t for _, it in candidatos for t in it.tickets):
        if quotes[t.key].fetched_at >= recente:
            continue
        pause(cfg)
        try:
            save_quote(db, t.key, *fetch(t, cfg))
        except Exception as e:
            log(f"Não consegui confirmar {t.label()}: {type(e).__name__}: {e}")
            falharam.add(t.key)
    quotes = load_quotes(db)
    preco_max = cfg["alerta"]["preco_maximo"]
    out = []
    for _, it in candidatos:
        qs = [quotes[t.key] for t in it.tickets]
        if any(t.key in falharam for t in it.tickets) or not all(q.price for q in qs):
            continue
        total = sum(q.price for q in qs)
        if total <= preco_max:
            out.append((total, it))
    out.sort(key=lambda x: x[0])
    return out


def describe(total: int, it: Itinerary, quotes: dict[str, Quote]) -> str:
    linhas = [
        f"{reais(total)} — parada em {it.hub or '-'} | sai de GRU {br(it.saida)}, {it.hub}→Tóquio "
        f"{br(it.ida_japao)}, volta de Tóquio {br(it.volta)} | {it.estrategia}"
    ]
    for t in it.tickets:
        q = quotes[t.key]
        linhas.append(f"    {t.label()}: {reais(q.price)} {', '.join(q.details.get('cias', []))}")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# E-mail
# ---------------------------------------------------------------------------


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    path = BASE / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def apply_overrides(cfg: dict, env: dict[str, str]) -> None:
    """Deixa mudar os critérios principais por Variables do GitHub, sem editar o config.toml."""
    if v := env.get("PRECO_MAXIMO"):
        cfg["alerta"]["preco_maximo"] = int(v)
    if v := env.get("SAIDA_DE"):
        cfg["viagem"]["saida_de"] = date.fromisoformat(v)
    if v := env.get("SAIDA_ATE"):
        cfg["viagem"]["saida_ate"] = date.fromisoformat(v)
    if v := env.get("DIAS_NO_JAPAO"):
        cfg["viagem"]["dias_no_japao"] = [int(x) for x in v.split(",")]
    if v := env.get("PARADAS"):
        cfg["paradas"]["cidades"] = [c.strip().upper() for c in v.split(",") if c.strip()]


def notifier() -> apprise.Apprise:
    env = load_env()
    url = env.get("APPRISE_URL")
    if not url:
        # Mesmos nomes de Secrets do monitor do Yosemite.
        user, senha, destino = env.get("EMAIL_FROM"), env.get("EMAIL_PASS"), env.get("EMAIL_TO")
        if not (user and senha):
            raise SystemExit("E-mail não configurado: defina EMAIL_FROM, EMAIL_PASS e EMAIL_TO.")
        login = user.split("@")[0]
        senha = senha.replace(" ", "")  # o Google mostra a senha de app em blocos de 4
        url = f"mailtos://{quote(login, safe='')}:{quote(senha, safe='')}@gmail.com?to={quote(destino or user)}"
    ap = apprise.Apprise()
    if not ap.add(url):
        raise SystemExit("URL de notificação inválida no .env.")
    return ap


def email_html(cfg: dict, opcoes: list[tuple[int, Itinerary]], quotes: dict[str, Quote], ref: list) -> str:
    e = html.escape
    partes = [
        f"<h2>Passagens abaixo de {reais(cfg['alerta']['preco_maximo'])}</h2>",
        "<p>Preços confirmados agora há pouco no Google Flights, para "
        f"{cfg['viagem']['adultos']} adulto(s), classe econômica. Confira no link antes de comprar.</p>",
    ]
    for total, it in opcoes:
        partes.append(
            f"<h3>{reais(total)} — parada em {e(it.hub)}</h3>"
            f"<p>Sai de GRU {br(it.saida)} · {e(it.hub)}→Tóquio {br(it.ida_japao)} · "
            f"volta de Tóquio {br(it.volta)}<br>"
            f"<i>{e(it.estrategia)}</i></p><ul>"
        )
        for t in it.tickets:
            q = quotes[t.key]
            voos = "<br>".join(e(v) for v in q.details.get("voos", []))
            obs = " (voos de ida; escolha a volta no link)" if len(t.legs) == 2 else ""
            partes.append(
                f"<li><a href='{e(t.query(cfg).url())}'>{e(t.label())}</a>: <b>{reais(q.price)}</b> "
                f"{e(', '.join(q.details.get('cias', [])))}{obs}<br><small>{voos}</small></li>"
            )
        partes.append("</ul>")
    if ref:
        total, it = ref[0]
        partes.append(
            f"<p>Referência: ida e volta sem parada mais barata está em {reais(total)} "
            f"({br(it.saida)}–{br(it.volta)}).</p>"
        )
    partes.append(
        "<p><small>São bilhetes separados: se um voo atrasar, a companhia do bilhete seguinte não tem "
        "obrigação de remarcar. A parada nos EUA exige visto americano válido.</small></p>"
    )
    return "\n".join(partes)


def send_alert(cfg: dict, db: sqlite3.Connection, opcoes: list[tuple[int, Itinerary]], ref: list) -> None:
    quotes = load_quotes(db)
    melhor = opcoes[0][0]
    ok = notifier().notify(
        title=f"✈️ GRU → EUA → Tóquio por {reais(melhor)}",
        body=email_html(cfg, opcoes, quotes, ref),
        body_format=apprise.NotifyFormat.HTML,
    )
    if not ok:
        raise SystemExit("Falha ao enviar o e-mail (confira EMAIL_FROM e EMAIL_PASS nos Secrets ou no .env).")
    ts = now().isoformat()
    db.executemany(
        "INSERT OR REPLACE INTO alerts VALUES (?, ?, ?)", [(it.key, total, ts) for total, it in opcoes]
    )
    db.commit()
    log(f"E-mail enviado com {len(opcoes)} opção(ões), a melhor por {reais(melhor)}.")


# ---------------------------------------------------------------------------
# Resumo na página da execução do GitHub Actions
# ---------------------------------------------------------------------------


def summary(lines: list[str]) -> None:
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n\n")


def summary_lines(cfg, feitas, sucessos, validos, total_buscas, top, ref, quotes) -> list[str]:
    preco_max = cfg["alerta"]["preco_maximo"]
    lines = [
        f"## ✈️ GRU → EUA → Tóquio (alerta até {reais(preco_max)})",
        "",
        f"Buscas nesta execução: {sucessos}/{feitas} sem erro · cobertura: {validos}/{total_buscas} com preço recente",
        "",
    ]
    if top:
        lines += ["| Total | Parada | Sai de GRU | EUA→Tóquio | Volta | Bilhetes |", "|---|---|---|---|---|---|"]
        for total, it in top:
            bilhetes = "<br>".join(f"{t.label()}: {reais(quotes[t.key].price)}" for t in it.tickets)
            lines.append(
                f"| **{reais(total)}** | {it.hub} | {br(it.saida)} | {br(it.ida_japao)} | {br(it.volta)} | {bilhetes} |"
            )
    else:
        lines.append("Ainda não há combinação completa com preços recentes.")
    if ref:
        lines += ["", f"Referência sem parada: {reais(ref[0][0])} ({br(ref[0][1].saida)}–{br(ref[0][1].volta)})"]
    if not top or top[0][0] > preco_max:
        lines += ["", f"Nenhuma combinação até {reais(preco_max)} por enquanto."]
    return lines


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=BASE / "config.toml", type=Path)
    ap.add_argument("--max-consultas", type=int, help="buscas nesta execução (padrão: config)")
    ap.add_argument("--paradas", help="limita as cidades de parada nesta execução, ex.: LAX,SFO")
    ap.add_argument("--sem-email", action="store_true", help="só mostra o resultado, não envia e-mail")
    ap.add_argument("--testar-email", action="store_true", help="envia um e-mail de teste e sai")
    args = ap.parse_args()

    if args.testar_email:
        ok = notifier().notify(title="Teste do monitor de passagens", body="Se chegou, o e-mail está configurado.")
        raise SystemExit(0 if ok else "Falha ao enviar o e-mail de teste.")

    cfg = tomllib.loads(args.config.read_text())
    apply_overrides(cfg, load_env())
    hubs = args.paradas.upper().split(",") if args.paradas else cfg["paradas"]["cidades"]
    db = open_db()
    prune_history(db)
    com_parada, referencia = build_itineraries(cfg, hubs)
    # Bilhetes com parada vêm antes da referência para terem prioridade nas buscas.
    tickets = list(dict.fromkeys(t for it in com_parada + referencia for t in it.tickets))
    limite = cfg["busca"]["consultas_por_execucao"] if args.max_consultas is None else args.max_consultas
    feitas, sucessos = refresh(db, cfg, tickets, limite)
    if feitas and not sucessos:
        summary([f"## ❌ Todas as {feitas} buscas falharam", "", "O Google pode ter bloqueado ou mudado a página."])
        raise SystemExit("Todas as buscas falharam; veja o log acima.")

    quotes = load_quotes(db)
    max_age = timedelta(hours=cfg["busca"]["validade_horas"])
    ranked = best_per_trip(evaluate(com_parada, quotes, max_age))
    ref = evaluate(referencia, quotes, max_age)
    validos = sum(1 for t in tickets if t.key in quotes and quotes[t.key].fetched_at >= now() - max_age)
    log(f"Cobertura: {validos}/{len(tickets)} buscas com preço recente; {len(ranked)} combinações completas.")
    for total, it in ranked[:5]:
        log("\n" + describe(total, it, quotes))
    if ref:
        log(f"Referência sem parada: {reais(ref[0][0])} ({br(ref[0][1].saida)}–{br(ref[0][1].volta)})")

    preco_max = cfg["alerta"]["preco_maximo"]
    summary(summary_lines(cfg, feitas, sucessos, validos, len(tickets), ranked[:5], ref, quotes))
    candidatos = [x for x in ranked if x[0] <= preco_max][: cfg["alerta"]["max_opcoes_no_email"]]
    if not candidatos:
        log(f"Nenhuma combinação até {reais(preco_max)} por enquanto.")
        return
    confirmados = confirm(db, cfg, candidatos)
    ultimo = dict(db.execute("SELECT itinerary, price FROM alerts"))
    melhoria = cfg["alerta"]["melhoria_minima"]
    novos = [(tot, it) for tot, it in confirmados if it.key not in ultimo or tot <= ultimo[it.key] - melhoria]
    if not novos:
        log("Combinações dentro do preço já foram avisadas (ou não se confirmaram).")
    elif args.sem_email:
        log(f"--sem-email: {len(novos)} opção(ões) seriam enviadas, a melhor por {reais(novos[0][0])}.")
    else:
        send_alert(cfg, db, novos, ref)
        summary([f"📧 **E-mail enviado** com {len(novos)} opção(ões), a melhor por {reais(novos[0][0])}."])


if __name__ == "__main__":
    main()
