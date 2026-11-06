"""The catalogue's own two views, and what keeps their identity honest.

The same three claims the other StratiGraph faces make (field assistant, room
browser, node console) — same brand, same reasoning:

* nothing is fetched at runtime: the theme, the faces and the marks are VENDORED
  (`sync-brand.sh`) and served same-origin;
* the stylesheet uses ROLES, not hexes;
* no text on a pure accent — the theme ships the inks that can carry text.

The READER this service also mounts is NOT covered: it is EMStudio's built
artefact, an Extended Matrix tool, and it keeps the EM look.
"""

from __future__ import annotations

import pathlib
import re

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
BRAND = APP / "brand"
PAGE = APP / "ui" / "index.html"
SHEET = APP / "ui" / "catalog.css"

GUIDEBOOK = {"#F1EBE3", "#D9D1CF", "#383838", "#2E2D2C", "#C4B282", "#8A8021",
             "#A64724", "#E85B1A", "#CAD531", "#4AA7D9", "#1E275C"}
PURE_ACCENTS = ("--sg-info", "--sg-ok", "--sg-accent")


def test_the_brand_is_vendored_here():
    assert (BRAND / "stratigraph-theme.css").is_file(), "run ./sync-brand.sh"
    assert len(sorted((BRAND / "fonts").glob("*.woff2"))) == 8
    assert (BRAND / "logo" / "favicon-deep-charcoal.svg").is_file()


def test_the_vendored_brand_is_the_SHARED_one_and_was_not_edited_here():
    """One source of truth. A theme tweaked in a consumer is a fork nobody
    called a fork."""
    here = (BRAND / "stratigraph-theme.css").read_text(encoding="utf-8")
    shared = (pathlib.Path(__file__).resolve().parent.parent.parent
              / "stratigraph-brand" / "stratigraph-theme.css")
    if not shared.is_file():                    # no sibling checkout in CI
        # the shared theme names itself in its first lines (6 Nov 2026: the
        # header says «the shared theme», no longer the repository's name)
        assert "the shared theme" in here[:400] or "stratigraph-brand" in here
        return
    assert here == shared.read_text(encoding="utf-8"), \
        "app/brand/ has drifted from stratigraph-brand/ — re-run ./sync-brand.sh"


def test_the_page_reaches_no_cdn():
    for path in (PAGE, SHEET, APP / "ui" / "catalog.js"):
        code = re.sub(r"/\*.*?\*/|<!--.*?-->", "", path.read_text(encoding="utf-8"),
                      flags=re.S)
        for host in ("fonts.googleapis.com", "fonts.gstatic.com",
                     "api.fontshare.com", "cdn.fontshare.com",
                     "cdn.jsdelivr.net", "unpkg.com"):
            assert host not in code, f"{path.name} reaches {host}"


def test_the_theme_is_imported_relatively_so_it_survives_the_proxy():
    assert '@import url("../brand/stratigraph-theme.css")' in \
        SHEET.read_text(encoding="utf-8")


def test_the_stylesheet_names_no_colour():
    found = re.findall(r"#[0-9A-Fa-f]{3,8}\b", SHEET.read_text(encoding="utf-8"))
    assert not found, found


def test_the_only_hexes_in_the_page_are_the_two_theme_colors():
    source = PAGE.read_text(encoding="utf-8")
    found = {h.upper() for h in re.findall(r"#[0-9A-Fa-f]{3,8}\b", source)}
    assert found <= {"#F1EBE3", "#2E2D2C"} <= GUIDEBOOK | found
    for hexed in found:
        assert f'content="{hexed}"' in source


def test_no_text_sits_on_a_pure_accent():
    for line in SHEET.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(("*", "/*", "//")) or "color-mix" in stripped:
            continue
        for accent in PURE_ACCENTS:
            assert not re.search(rf"(?<!-)\bcolor:\s*var\({accent}\)", stripped), \
                f"text on a pure accent — {stripped}"


def test_a_filled_button_is_charcoal_because_burnt_cannot_carry_text_this_size():
    sheet = SHEET.read_text(encoding="utf-8")
    assert "--fill:     var(--sg-deep-charcoal)" in sheet
    assert "--on-fill:  var(--sg-off-white)" in sheet


def test_the_page_wears_the_hourglass_and_the_display_face():
    source = PAGE.read_text(encoding="utf-8")
    assert "favicon-deep-charcoal.svg" in source
    assert 'class="wordmark">StratiGraph' in source
    assert "font-family: var(--sg-font-display)" in SHEET.read_text(encoding="utf-8")


def test_the_static_mounts_revalidate_rather_than_go_stale():
    """Ported from StratiGraph Server, where it was measured: ETag but no
    `Cache-Control` means a browser applies heuristic freshness and serves a
    stale stylesheet to a tab that is already open."""
    main = (APP / "main.py").read_text(encoding="utf-8")
    assert "class _FreshStatic(StaticFiles)" in main
    assert 'response.headers.setdefault("Cache-Control", "no-cache")' in main
    assert main.count("_FreshStatic(directory=") == 2, \
        "every StratiGraph-native mount must revalidate"


def test_the_views_read_the_PUBLIC_api_and_KEEP_NO_CREDENTIAL_ANYWHERE():
    """A catalogue whose purpose is discovery answers an anonymous caller.

    ── PERCHÉ QUESTA GUARDIA È CAMBIATA IL 7 OTTOBRE 2026, e cosa afferma adesso

    Diceva `"Authorization" not in js`. Era giusta finché questa pagina non
    aveva verbi: `DELETE /catalog/study/{id}` era scritto, testato e in piedi, e
    **nessun browser lo poteva chiamare** — non per un bottone mancante, ma
    perché la sola superficie che mostra uno studio non aveva modo di essere
    qualcuno.

    Allargare una guardia per far entrare del codice nuovo è il modo in cui una
    guardia diventa una formalità, quindi il confine è stato spostato e non
    togliesto, e adesso afferma **quattro cose invece di una**:

    1. leggere resta ANONIMO — le due viste chiamano `/studies?` e non
       richiedono niente;
    2. nessuna credenziale su DISCO: né `localStorage`, né `sessionStorage`, né
       un cookie. *(Il verificatore PKCE sta in `sessionStorage` e vive in
       `auth.js`, che è vendorizzato e porta la propria ragione: sopravvive al
       rimbalzo sull'IdP e viene cancellato appena il codice è speso.)*
    3. nessun token in una URL — `list_studies` accetta `?token=` per i banchi
       di prova, e una pagina che lo usasse metterebbe una credenziale nella
       barra dell'indirizzo, nella cronologia e in ogni schermata condivisa;
    4. `Authorization` compare in un posto solo, e in un'INTESTAZIONE.

    ── E IL PAGLIAIO, DICHIARATO (§5)

    È un sorgente letto come testo, perché questa pagina non si può importare —
    tocca `document` al primo livello. Il falso positivo costruibile sarebbe un
    COMMENTO che nomina uno dei recipienti: è la riga onesta che ha morso
    davvero in `check-members` il 5 ottobre. Quindi la prosa si toglie prima, e
    le prove qui sotto lo dimostrano invece di prometterlo.
    """
    raw = (APP / "ui" / "catalog.js").read_text(encoding="utf-8")
    js = _senza_prosa(raw)

    assert "/studies?" in js, "le due viste non chiamano più l'elenco pubblico"

    #: 2 · niente su disco
    for sink in ("localStorage", "sessionStorage", "document.cookie"):
        assert sink not in js, (
            f"{sink} in catalog.js: una credenziale su disco sopravvive alla "
            f"persona alla tastiera")

    #: 3 · niente token in una query, in nessuna delle grafie che qualcuno
    #: scriverebbe
    for shape in ("token=", "?token", "&token"):
        assert shape not in js, f"un token in una URL ({shape})"

    #: 4 · e l'unico `Authorization` è un'intestazione, in un posto solo
    assert js.count("Authorization") == 1, (
        f"«Authorization» compare {js.count('Authorization')} volte: il token "
        f"è affare di UNA funzione")
    at = js.index("Authorization")
    assert "headers" in js[max(0, at - 400):at], (
        "«Authorization» non è dentro le intestazioni di una richiesta")


def test_E_LA_GUARDIA_QUI_SOPRA_MORDE_ANCORA_su_un_deposito_vero():
    """La prima delle due prove: il caso che la fa scattare."""
    finto = 'const t = 1;\nlocalStorage.setItem("sg.token", token);\n'
    assert "localStorage" in _senza_prosa(finto)
    finto_url = 'const u = `${BASE}/studies?token=${token}`;\n'
    assert "token=" in _senza_prosa(finto_url)


def test_E_NON_MORDE_PIU_su_un_commento_che_spiega_la_regola():
    """La seconda: la riga onesta. È quella che il 5 ottobre ha fatto fallire un
    recinto in EMStudio — il messaggio del recinto, scritto come commento."""
    onesto = ('// il token NON va in localStorage e mai in ?token= nella URL\n'
              'const x = 1;\n')
    spogliato = _senza_prosa(onesto)
    assert "localStorage" not in spogliato
    assert "token=" not in spogliato


def _senza_prosa(source: str) -> str:
    """I commenti, tolti. **Igiene, non una forza**: non risponde niente su un
    programma, e nei tre gemelli (`stratigraph-server/tests/sorgenti.py`,
    `EMStudio/frontend/scripts/sorgenti.mjs`,
    `stratigraph-chatbot/tests/sorgenti.py`) otto morsi su nove erano su codice
    vero, non su commenti.

    Questo repo è il QUARTO senza lettore condiviso, ed è scritto nel referto
    del 7 ottobre invece di essere una quarta copia silenziosa.
    """
    import re as _re
    return _re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", source)
