import os
import sys
import json
import re
import unicodedata
import random
import argparse
import html
import urllib.request
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo("Europe/Rome")
except Exception:
    # Fallback grezzo se manca il database dei fusi (Windows: pip install tzdata)
    FUSO = timezone(timedelta(hours=1))

import feedparser

# ========================= CONFIGURAZIONE =========================
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
STATO_FILE = "gia_postati.json"

ORE_ATTIVE = {9, 12, 15, 18, 21}           # ore (di Roma) in cui il bot agisce: ogni 3 ore
FRESCHEZZA_ORE = 3        # "super fresh": pubblicata nelle ultime 3 ore
MEMORIA_GIORNI = 7        # per quanto tempo ricordare gli URL già postati
TOP_K = 3                 # fra quanti candidati fare la scelta pesata-casuale
SOGLIA_SIMILI = 0.55      # somiglianza fra titoli per dire "è la stessa notizia"
RICHIEDI_WHITELIST = True # se True, scarta chi non contiene alcun tema della whitelist

FEEDS = [
    {"fonte": "ANSA",         "ambito": "italia",         "url": "https://www.ansa.it/sito/notizie/topnews/topnews_rss.xml"},
    {"fonte": "France 24",    "ambito": "internazionale", "url": "https://www.france24.com/en/rss"},
    {"fonte": "BBC",          "ambito": "internazionale", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"fonte": "The Guardian", "ambito": "internazionale", "url": "https://www.theguardian.com/world/rss"},
]

# ---- WHITELIST: temi da tenere (basta che ne compaia uno) ----
# Scritta SENZA accenti e a RADICI, in italiano / inglese / francese, perché il testo
# viene confrontato dopo averlo messo in minuscolo e tolto gli accenti.
# (es. "election" prende election/élection; "economi" prende economia/economy/économie)
# Tarata sui 263 articoli reali della cartella Circolati (analisi delle frequenze).
WHITELIST = [
    # Istituzioni e politica (ruoli, organi)
    "govern", "gouvern", "presiden", "presidenzial", "presidentiel",
    "minist", "premier", "cancellier", "chancellor", "chancelier",
    "parlament", "parliament", "parlement", "senat", "senate", "deput",
    "maggioranz", "majorit", "opposizion", "opposition", "coalizion", "coalition",
    "leader", "potere", "pouvoir", "power",
    # Voto ed elezioni
    "elezion", "election", "scrutin", "referendum", "vote", "voto", "votaz",
    "legislativ", "elettoral", "electoral", "sondagg", "sondage",
    # Leggi, giustizia, riforme
    "sentenz", "tribunal", "verdict", "diritt", "rights", "droit",
    "decreto", "decret", "decree", "riforma", "reforme", "reform",
    "commission", "indagin", "inchiest", "enquete", "procura",
    # Organizzazioni internazionali, diplomazia, vertici
    "organizzazion", "organisation", "organization", "istituzion", "institution",
    "vertice", "summit", "sommet", "diplomat", "diplomaz",
    "trattat", "treaty", "traite", "cooperazion", "cooperation",
    "alleanz", "alliance", "accord", "negoziat", "negociation",
    "unione europea", "european union", "union europeenne", "commissione europea",
    "nazioni unite", "united nations", "nations unies",
    # Conflitti, difesa ed esteri
    "guerr", "conflitt", "conflict", "crisi", "crise", "crisis",
    "difes", "defens", "defence", "militar", "militair", "armee", "forze armate",
    "sicurezz", "securit", "security",
    "ucrain", "russi", "gaza", "israel", "palestin", "hamas", "iran",
    "medio oriente", "middle east", "moyen orient", "sanzion", "sanction",
    "migrant", "immigr", "profug", "refug", "frontier", "border",
    # Luoghi / attori chiave
    "stati uniti", "etats unis", "united states", "casa bianca", "white house", "maison blanche",
    "kiev", "mosca", "moscou", "moscow", "cina", "chine", "china", "washington",
    "estrema destra", "extreme droite", "far right",
    # Economia e lavoro
    "economi", "inflaz", "inflation", "budget", "manovra", "bilancio",
    "dazi", "tariff", "tarif", "recession", "impres", "entrepris", "aziend", "business",
    "lavoro", "occupazion", "emploi", "mercat",
    # Società, proteste, temi
    "protest", "manifestation", "demonstr", "sciopero", "strike", "greve",
    "clima", "climate", "climat", "europ",
    "intelligenza artificiale", "intelligence artificielle", "artificial intelligence",
]

# Sigle e parole corte/ambigue + nomi propri: match a PAROLA INTERA, altrimenti
# "nato" prenderebbe "senato"/"designato", "conte" prenderebbe "contenuto", ecc.
WHITELIST_SIGLE = [
    # sigle di organizzazioni
    "nato", "otan", "ue", "eu", "onu", "bce", "ecb", "fmi", "imf", "omc", "wto",
    "osce", "oms", "who", "g7", "g8", "g20", "unesco", "ocse", "oecd", "fbi", "cia",
    # parole corte ambigue
    "war", "court", "corte",
    # figure politiche di spicco (parola intera; da aggiornare col tempo)
    "meloni", "salvini", "conte", "schlein", "renzi", "tajani", "mattarella", "draghi",
    "macron", "trump", "putin", "zelensky", "netanyahu", "biden", "orban", "bolsonaro", "lula",
]

# ---- BLACKLIST: spazzatura da buttare sempre (basta una) ----
BLACKLIST = [
    "oroscopo", "horoscope", "meteo", "weather", "ricetta", "recipe",
    "calcio", "football", "soccer", "serie a", "premier league", "champions", "gol", "goal",
    "pagelle", "classifica", "formula 1", "motogp", "tennis", "basket", "nba", "nfl",
    "gossip", "royal", "celebrity", "celebrite", "lifestyle", "moda", "fashion", "box office",
]


# ========================= FUNZIONI DI SUPPORTO =========================
def e_parola_breve(p):
    return len(p) <= 3


def contiene_una(testo, parole):
    """True se `testo` contiene almeno una delle `parole`.
    Per le parole corte (<=3, es. 'ue') cerca la parola intera, per evitare
    falsi positivi come 'ue' dentro 'tuesday'. Per le altre basta la sottostringa."""
    for p in parole:
        p = p.lower()
        if e_parola_breve(p):
            if re.search(rf"\b{re.escape(p)}\b", testo):
                return True
        elif p in testo:
            return True
    return False


def senza_accenti(s):
    """Minuscolo e senza accenti: così 'élection' diventa 'election' e le liste
    si possono scrivere senza accenti, uguali per italiano/inglese/francese."""
    s = s.lower()
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def passa_whitelist(testo):
    """True se il testo (già normalizzato) tocca almeno un tema.
    Le radici si cercano come sottostringa; le sigle/nomi come parola intera."""
    if any(p in testo for p in WHITELIST):
        return True
    return any(re.search(rf"\b{re.escape(s)}\b", testo) for s in WHITELIST_SIGLE)


def normalizza(t):
    """Riduce un titolo a lettere e spazi, per confrontare i titoli fra loro (risonanza)."""
    t = senza_accenti(t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def pulisci_url(u):
    u = (u or "").strip()
    return u.split("?utm_")[0] if "?utm_" in u else u


def eta_ore(entry):
    """Ore trascorse dalla pubblicazione, o None se la data manca."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    pubblicato = datetime(*t[:6], tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - pubblicato).total_seconds() / 3600


# ========================= STATO (memoria fra un giro e l'altro) =========================
def carica_stato():
    try:
        with open(STATO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"postati": {}, "last_source": ""}


def salva_stato(stato):
    # Tiene solo gli URL postati di recente, così il file non cresce all'infinito
    limite = datetime.now(timezone.utc) - timedelta(days=MEMORIA_GIORNI)
    stato["postati"] = {
        u: t for u, t in stato["postati"].items()
        if datetime.fromisoformat(t) > limite
    }
    with open(STATO_FILE, "w", encoding="utf-8") as f:
        json.dump(stato, f, ensure_ascii=False, indent=2)


# ========================= RACCOLTA E SELEZIONE =========================
def raccogli_candidati(gia_postati):
    """Legge tutti i feed e tiene solo gli articoli freschi, nuovi e pertinenti."""
    candidati = []
    for feed in FEEDS:
        d = feedparser.parse(feed["url"])
        if d.bozo or not d.entries:
            print(f"[!] feed sospetto o vuoto: {feed['fonte']} ({feed['url']})")
            continue

        for e in d.entries:
            url = pulisci_url(e.get("link", ""))
            if not url or url in gia_postati:
                continue

            eta = eta_ore(e)
            if eta is None or eta > FRESCHEZZA_ORE:   # filtro freschezza
                continue

            titolo = html.unescape(e.get("title", "")).strip()
            grezzo = titolo + " " + html.unescape(e.get("summary", ""))
            testo = senza_accenti(grezzo)              # minuscolo + senza accenti, per i confronti

            if contiene_una(testo, BLACKLIST):         # scarta la spazzatura
                continue
            if RICHIEDI_WHITELIST and not passa_whitelist(testo):
                continue

            candidati.append({
                "fonte": feed["fonte"], "ambito": feed["ambito"],
                "titolo": titolo, "url": url, "eta": eta, "testo": testo,
            })
    return candidati


def risonanza(item, tutti):
    """Quante testate diverse stanno battendo la stessa notizia (1 = sola).
    È il proxy gratuito dell'importanza: se BBC+Guardian+France24 la riprendono, è grossa.
    Nota: funziona soprattutto fra le fonti nella stessa lingua (le EN fra loro)."""
    fonti = {item["fonte"]}
    titolo_norm = normalizza(item["titolo"])
    for altro in tutti:
        if altro is item:
            continue
        if SequenceMatcher(None, titolo_norm, normalizza(altro["titolo"])).ratio() >= SOGLIA_SIMILI:
            fonti.add(altro["fonte"])
    return len(fonti)


def punteggio(item, tutti):
    recency = max(0.0, FRESCHEZZA_ORE - item["eta"])          # più fresca = più alto
    ris = risonanza(item, tutti)                               # 1..n fonti
    boost = sum(1 for p in WHITELIST if p in item["testo"])    # quanti temi tocca
    return recency + (ris - 1) * 2.0 + boost * 0.5


def scegli(candidati, last_source):
    """Dà un punteggio a tutti, prende i migliori TOP_K e ne estrae uno a caso,
    pesato sul punteggio e con un freno sulla fonte usata l'ultima volta (varietà)."""
    if not candidati:
        return None
    for c in candidati:
        c["score"] = punteggio(c, candidati)
    candidati.sort(key=lambda c: c["score"], reverse=True)

    top = candidati[:TOP_K]
    pesi = []
    for c in top:
        peso = c["score"]
        if c["fonte"] == last_source:
            peso *= 0.4                       # evita di ripescare sempre la stessa testata
        pesi.append(max(peso, 0.01))
    return random.choices(top, weights=pesi, k=1)[0]


# ========================= INVIO =========================
def invia_discord(item, webhook):
    testo = f"**{item['fonte']}** · {item['titolo']}\n{item['url']}"
    payload = json.dumps({"content": testo}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"Discord ha risposto {resp.status}")


# ========================= MAIN =========================
def main():
    parser = argparse.ArgumentParser(description="Bot RSS breaking news -> Discord")
    parser.add_argument("--prova", action="store_true",
                        help="Invia sul webhook di prova (DISCORD_WEBHOOK_PROVA) e ignora l'orario.")
    args = parser.parse_args()

    # Scelta del webhook: in prova si usa il canale di test
    webhook = os.getenv("DISCORD_WEBHOOK_PROVA") if args.prova else WEBHOOK_URL
    if not webhook:
        manca = "DISCORD_WEBHOOK_PROVA" if args.prova else "DISCORD_WEBHOOK"
        print(f"{manca} non impostato.")
        sys.exit(1)

    # Cancello orario: cron è in UTC, quindi è il codice a controllare l'ora di Roma.
    # In prova si ignora l'orario, così puoi testare a qualsiasi ora.
    ora = datetime.now(FUSO).hour
    if not args.prova and ora not in ORE_ATTIVE:
        print(f"Ora di Roma = {ora}: fuori fascia (9-21 ogni 3h). Esco.")
        return

    stato = carica_stato()
    candidati = raccogli_candidati(stato["postati"])
    print(f"Candidati freschi e pertinenti: {len(candidati)}")

    scelto = scegli(candidati, stato.get("last_source", ""))
    if scelto is None:
        print("Nessuna notizia da inviare in questo giro.")
        return

    invia_discord(scelto, webhook)
    canale = "PROVA" if args.prova else "normale"
    print(f"Inviato ({canale}): [{scelto['fonte']}] {scelto['titolo']}  (score {scelto['score']:.1f})")

    # La memoria si aggiorna solo per gli invii veri, così il canale di prova
    # non "consuma" le notizie del canale normale.
    if not args.prova:
        stato["postati"][scelto["url"]] = datetime.now(timezone.utc).isoformat()
        stato["last_source"] = scelto["fonte"]
        salva_stato(stato)


if __name__ == "__main__":
    main()
