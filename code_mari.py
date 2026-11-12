#!/usr/bin/env python3
"""Pipeline autonome de téléchargement du comic "Code Mari".

Parcourt l'œuvre page après page à partir de l'état mémorisé dans
avancement.json et range chaque image dans images/ChXX/XXX_NomFichier.

Usage :
    ./code_mari.py                  # reprend là où le dernier lancement s'est arrêté
    ./code_mari.py --max-pages 10   # s'arrête après 10 pages
    ./code_mari.py --root /autre/dossier

Aucune dépendance : bibliothèque standard Python 3 uniquement.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

DEFAULT_ROOT = "/home/joel/Code_Mari"
CONFIG_NAME = "avancement.json"
DEFAULT_CONFIG = {
    "start_url": "https://palladcity.com/comic/code-mari-chapter-1/",
    "last_url": "",
    "page_number": 1,
    "art_name": "Code Mari",
}
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class PipelineError(Exception):
    """Erreur bloquante : la boucle s'arrête, l'état n'est pas avancé."""


# ---------------------------------------------------------------- DOM minimal

class Node:
    def __init__(self, tag, attrs, parent=None):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children = []  # Node ou str

    def iter(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find(self, tag, attr=None, value=None):
        for node in self.iter():
            if node.tag != tag:
                continue
            if attr is None:
                return node
            current = node.attrs.get(attr) or ""
            if attr == "class":
                if value in current.split():
                    return node
            elif current == value:
                return node
        return None

    def find_all(self, tag):
        return [n for n in self.iter() if n.tag == tag]

    def text(self):
        parts = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return "".join(parts)


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, dict(attrs), self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:  # balise fermante orpheline : ignorée
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def parse_html(html):
    builder = TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


# -------------------------------------------------------------------- réseau

def fetch(url, retries=3, timeout=30):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers.get_content_charset()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500 and exc.code != 429:
                break  # erreur définitive (404, 403...) : inutile de réessayer
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(2 * attempt)
    raise PipelineError(f"téléchargement impossible de {url} : {last_error}")


# ------------------------------------------------------------- configuration

def load_config(path):
    """Lit avancement.json ; le crée avec les valeurs par défaut s'il n'existe pas."""
    if not os.path.exists(path):
        save_config(path, DEFAULT_CONFIG)
        print(f"Fichier {path} créé.")
    with open(path, encoding="utf-8") as handle:  # relu même s'il vient d'être créé
        config = json.load(handle)
    missing = [key for key in DEFAULT_CONFIG if key not in config]
    if missing:
        raise PipelineError(f"{path} : propriétés manquantes : {', '.join(missing)}")
    config["page_number"] = int(config["page_number"])
    return config


def save_config(path, config):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=4, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)  # écriture atomique : jamais de fichier à moitié écrit


# ------------------------------------------------------- analyse d'une page

def image_source(img):
    """URL de l'image, en tenant compte du chargement différé (lazy-load)."""
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        value = (img.attrs.get(attr) or "").strip()
        if value and not value.startswith("data:"):
            return value
    return None


def parse_page(html, page_url, art_name):
    """Retourne (image_url, image_filename, next_url, chapter_number)."""
    dom = parse_html(html)

    # Image principale : le <img> dans <div id="comic">.
    comic = dom.find("div", "id", "comic")
    if comic is None:
        raise PipelineError('<div id="comic"> introuvable')
    image_url = None
    for img in comic.find_all("img"):
        source = image_source(img)
        if source:
            image_url = urljoin(page_url, source)
            break
    if image_url is None:
        raise PipelineError('aucun <img> exploitable dans <div id="comic">')
    image_filename = os.path.basename(unquote(urlsplit(image_url).path))
    if not image_filename:
        raise PipelineError(f"nom de fichier introuvable dans {image_url}")

    # Page suivante : le <a> contenant "Next" dans <table id="comic-nav-wrapper">.
    next_url = ""
    nav = dom.find("table", "id", "comic-nav-wrapper")
    if nav is not None:
        for link in nav.find_all("a"):
            href = (link.attrs.get("href") or "").strip()
            if "Next" in link.text() and href:
                next_url = urljoin(page_url, href)
                break

    # Chapitre : <h2> dans <div class="post-info">, de la forme "Œuvre: Chapter N".
    post_info = dom.find("div", "class", "post-info")
    title = post_info.find("h2") if post_info is not None else None
    if title is None:
        raise PipelineError('<h2> introuvable dans <div class="post-info">')
    title_text = " ".join(title.text().split())
    parts = title_text.split(":")
    if len(parts) < 2:
        raise PipelineError(f"titre sans ':' : {title_text!r}")
    if parts[0].strip() != art_name:
        raise PipelineError(
            f"le titre {title_text!r} ne correspond pas à l'œuvre {art_name!r}"
        )
    match = re.search(r"(\d+)\s*$", parts[1])
    if match is None:
        raise PipelineError(f"numéro de chapitre introuvable dans {title_text!r}")

    return image_url, image_filename, next_url, int(match.group(1))


# ------------------------------------------------------------------- boucle

def run(root, max_pages, delay):
    config_path = os.path.join(root, CONFIG_NAME)
    images_root = os.path.join(root, "images")
    os.makedirs(images_root, exist_ok=True)

    config = load_config(config_path)
    current_url = config["last_url"] or config["start_url"]
    done = 0

    while current_url:
        if max_pages and done >= max_pages:
            print(f"Limite de {max_pages} page(s) atteinte.")
            break

        # 1. Lecture de la page
        body, charset = fetch(current_url)
        html = body.decode(charset or "utf-8", errors="replace")
        try:
            image_url, image_filename, next_url, chapter_number = parse_page(
                html, current_url, config["art_name"]
            )
        except PipelineError as exc:
            raise PipelineError(f"{current_url} : {exc}") from None

        # 2. Extraction & sauvegarde
        chapter_dir = os.path.join(images_root, f"Ch{chapter_number:02d}")
        os.makedirs(chapter_dir, exist_ok=True)
        target = os.path.join(chapter_dir, f"{config['page_number']:03d}_{image_filename}")
        data, _ = fetch(image_url)
        with open(target + ".part", "wb") as handle:
            handle.write(data)
        os.replace(target + ".part", target)
        print(f"[{config['page_number']:03d}] Ch{chapter_number:02d} <- {image_url}")
        done += 1

        # 3. Mise à jour de l'état
        if next_url:
            config["last_url"] = next_url
            config["page_number"] += 1
        save_config(config_path, config)
        current_url = next_url

        if current_url and delay > 0:
            time.sleep(delay)
    else:
        print("Dernière page atteinte : pas de lien « Next ».")

    print(f"{done} image(s) enregistrée(s) dans {images_root}")


def main():
    parser = argparse.ArgumentParser(description='Télécharge le comic "Code Mari" chapitre par chapitre.')
    parser.add_argument("--root", default=os.environ.get("CODE_MARI_ROOT", DEFAULT_ROOT),
                        help=f"répertoire racine (défaut : {DEFAULT_ROOT})")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="nombre maximal de pages pour ce lancement (0 = illimité)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="pause en secondes entre deux pages (défaut : 1)")
    args = parser.parse_args()

    try:
        run(args.root, args.max_pages, args.delay)
    except PipelineError as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrompu ; le prochain lancement reprendra à la dernière page enregistrée.",
              file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
