#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2019, 2020 Matt Post <post@cs.jhu.edu>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Ingests data into the Anthology. It takes a list of one or more
ACLPUB proceedings/ directories and does the following:

- executes some basic sanity checks
- applies normalization to names and titles (e.g, fixed-case protection)
- generates the nexted XML in the Anthology repository
- copies the PDFs and attachments into place for rsyncing to the server

Updated in March 2020, this script replaces:
- the old ingest.py (which converted the old ACLPUB flat XML format)
- anthologize.pl in ACLPUB
- anthology_xml.py in ACLPUB

"""

import argparse
import os
import re
import shutil
import sys

import lxml.etree as etree

from collections import defaultdict, OrderedDict
from datetime import datetime

from normalize_anth import normalize
from anthology.utils import (
    make_nested,
    make_simple_element,
    build_anthology_id,
    deconstruct_anthology_id,
    indent,
)
from anthology.index import AnthologyIndex
from anthology.people import PersonName
from anthology.bibtex import read_bibtex

from itertools import chain
from typing import Dict, Any


def log(text: str, fake: bool = False):
    message = "[DRY RUN] " if fake else ""
    print(f"{message}{text}", file=sys.stderr)


def read_meta(path: str) -> Dict[str, Any]:
    meta = {"chairs": []}
    with open(path) as instream:
        for line in instream:
            key, value = line.rstrip().split(" ", maxsplit=1)
            if key.startswith("chair"):
                meta["chairs"].append(value)
            else:
                meta[key] = value
    return meta


def bib2xml(bibfilename, anthology_id):
    """
    Moved here from ACLPUB's anthology_xml.py script.
    """

    fields = [
        'title',
        'author',
        'editor',
        'booktitle',
        'month',
        'year',
        'address',
        'publisher',
        'pages',
        'abstract',
        'url',
        'doi',
    ]

    # log(f"Reading {bibfilename}")
    collection_id, volume_name, paper_no = deconstruct_anthology_id(anthology_id)
    if paper_no == '':
        return  # skip the master bib file; we only process the individual files

    bibdata = read_bibtex(bibfilename)
    if len(bibdata.entries) != 1:
        log(f"more than one entry in {bibfilename}")
    bibkey, bibentry = bibdata.entries.items()[0]

    paper = make_simple_element("paper", attrib={"id": paper_no})
    for field in list(bibentry.fields) + list(bibentry.persons):
        if field not in fields:
            log(f"unknown field {field}")

    for field in fields:
        if field in ['author', 'editor']:
            if field in bibentry.persons:
                for person in bibentry.persons[field]:
                    first_text = ' '.join(person.bibtex_first_names)
                    last_text = ' '.join(person.prelast_names + person.last_names)
                    if person.lineage_names:
                        last_text += ', ' + ' '.join(person.lineage_names)

                    # Don't distinguish between authors that have only a first name
                    # vs. authors that have only a last name; always make it a last name.
                    if last_text.strip() in [
                        '',
                        '-',
                    ]:  # Some START users have '-' for null
                        last_text = first_text
                        first_text = ''

                    name_node = make_simple_element(field, parent=paper)
                    make_simple_element("first", first_text, parent=name_node)
                    make_simple_element("last", last_text, parent=name_node)
        else:
            if field == 'url':
                value = f"{anthology_id}"
                if 'url' in bibentry.fields and bibentry.fields['url'] != value:
                    log(f"rewriting url {bibentry.fields['url']} -> {value}")
            elif field in bibentry.fields:
                value = bibentry.fields[field]
            elif field == 'bibtype':
                value = bibentry.type
            elif field == 'bibkey':
                value = bibkey
            else:
                continue

            make_simple_element(field, text=value, parent=paper)

    return paper


def main(args):
    collections = defaultdict(OrderedDict)
    volumes = {}

    # Build list of volumes, confirm uniqueness
    for proceedings in args.proceedings:
        meta = read_meta(os.path.join(proceedings, "meta"))
        meta["path"] = proceedings

        meta["collection_id"] = collection_id = (
            meta["year"] + "." + meta["abbrev"].lower()
        )
        volume_name = meta["volume_name"]
        volume_full_id = f"{collection_id}-{volume_name}"

        if volume_full_id in volumes:
            print("Error: ")

        collections[collection_id][volume_name] = {}
        volumes[volume_full_id] = meta

    # Copy over the PDFs and attachments
    for volume, meta in volumes.items():
        root_path = os.path.join(meta["path"], "cdrom")
        collection_id = meta["collection_id"]
        venue_name = meta["abbrev"].lower()
        volume_name = meta["volume_name"]

        pdfs_dest_dir = os.path.join(args.pdfs_dir, venue_name)
        if not os.path.exists(pdfs_dest_dir):
            os.makedirs(pdfs_dest_dir)

        print(f"VOLUME: {volume}")

        # copy the book
        book_src_filename = meta["abbrev"] + "-" + meta["year"]
        book_src_path = os.path.join(root_path, book_src_filename) + ".pdf"
        book_dest_path = None
        if os.path.exists(book_src_path):
            book_dest_path = (
                os.path.join(pdfs_dest_dir, f"{collection_id}-{volume_name}") + ".pdf"
            )

            log(f"Copying {book_src_path} -> {book_dest_path}", args.dry_run)
            if not args.dry_run:
                shutil.copyfile(book_src_path, book_dest_path)

        # copy the paper PDFs
        pdf_src_dir = os.path.join(root_path, "pdf")
        for pdf_file in os.listdir(pdf_src_dir):
            # names are {abbrev}{number}.pdf
            abbrev = meta["abbrev"]
            match = re.match(rf"{abbrev}(\d+)\.pdf", pdf_file)

            if match is not None:
                paper_num = int(match[1])
                paper_id_full = f"{collection_id}-{volume_name}.{paper_num}"

                bib_path = os.path.join(
                    root_path,
                    "bib",
                    pdf_file.replace("/pdf", "/bib/").replace(".pdf", ".bib"),
                )

                pdf_src_path = os.path.join(pdf_src_dir, pdf_file)
                pdf_dest_path = os.path.join(
                    pdfs_dest_dir, f"{collection_id}-{volume_name}.{paper_num}.pdf"
                )
                log(
                    f"Copying [{paper_id_full}] {pdf_src_path} -> {pdf_dest_path}",
                    args.dry_run,
                )
                if not args.dry_run:
                    shutil.copyfile(pdf_src_path, pdf_dest_path)

                collections[collection_id][volume_name][paper_num] = {
                    "anthology_id": paper_id_full,
                    "bib": bib_path,
                    "pdf": pdf_dest_path,
                    "attachments": [],
                }

        # copy the attachments
        if os.path.exists(os.path.join(root_path, "additional")):
            attachments_dest_dir = os.path.join(args.attachments_dir, collection_id)
            if not os.path.exists(attachments_dest_dir):
                os.makedirs(attachments_dest_dir)
            for attachment_file in os.listdir(os.path.join(root_path, "additional")):
                match = re.match(rf"{abbrev}(\d+)_(\w+)\.(\w+)")
                if match is not None:
                    paper_num, type_, ext = match.groups()
                    paper_num = int(paper_num)

                    file_name = f"{collection_id}-{volume_name}.{paper_num}.{type_}.{ext}"
                    dest_path = os.path.join(attachments_dest_dir, file_name)
                    log(f"Copying {attachment_file} -> {dest_path}", args.dry_run)
                    if not args.dry_run:
                        shutil.copyfile(attachment_file, dest_path)

                    collections[collection_id][volume_name][paper_num][
                        "attachments"
                    ].append(dest_path)

    people = AnthologyIndex(
        None, srcdir=os.path.join(os.path.dirname(sys.argv[0]), "..", "data")
    )

    for collection_id, collection in collections.items():
        collection_file = os.path.join(
            args.anthology_dir, "data", "xml", f"{collection_id}.xml"
        )
        root_node = make_simple_element("collection", attrib={"id": collection_id})

        for volume_id, volume in collection.items():
            volume_node = make_simple_element(
                "volume", attrib={"id": volume_id}, parent=root_node
            )
            meta = None

            for paper_num, paper in sorted(volume.items()):
                paper_id_full = paper["anthology_id"]
                bibfile = paper["bib"]
                paper_node = bib2xml(bibfile, paper_id_full)
                # print(etree.tostring(paper_node, pretty_print=True))

                if paper_node.attrib["id"] == "0":
                    # create metadata subtree
                    meta = make_simple_element("meta", parent=volume_node)
                    title_node = paper_node.find("title")
                    title_node.tag = "booktitle"
                    meta.append(title_node)
                    for editor in paper_node.findall("editor"):
                        meta.append(editor)
                    meta.append(paper_node.find("publisher"))
                    meta.append(paper_node.find("address"))
                    meta.append(paper_node.find("month"))
                    meta.append(paper_node.find("year"))
                    if book_dest_path is not None:
                        make_simple_element(
                            "url", text=f"{collection_id}-{volume_name}", parent=meta
                        )

                    # modify frontmatter tag
                    paper_node.tag = "frontmatter"
                    del paper_node.attrib["id"]
                else:
                    # remove unneeded fields
                    for child in paper_node:
                        if child.tag in [
                            "editor",
                            "address",
                            "booktitle",
                            "publisher",
                            "year",
                            "month",
                        ]:
                            paper_node.remove(child)

                for attachment in paper["attachments"]:
                    make_simple_element(
                        "attachment",
                        text=attachment.path,
                        attrib={"type": attachment.type,},
                        parent=paper_node,
                    )

                if len(paper_node) > 0:
                    volume_node.append(paper_node)

        # Normalize
        for paper in root_node.findall(".//paper"):
            for oldnode in paper:
                normalize(oldnode, informat="latex")

        # Ensure names are properly identified
        ambiguous = {}
        for paper in root_node.findall(".//paper"):
            anth_id = build_anthology_id(
                collection_id, paper.getparent().attrib["id"], paper.attrib["id"]
            )

        for node in chain(paper.findall("author"), paper.findall("editor")):
            name = PersonName.from_element(node)
            ids = people.get_ids(name)
            if len(ids) > 1:
                print(
                    f"WARNING ({anth_id}): ambiguous author {name}, defaulting to first of {ids}"
                )
                ambiguous[anth_id] = (name, ids)

                node.attrib["id"] = ids[0]

        indent(root_node)
        tree = etree.ElementTree(root_node)
        tree.write(
            collection_file, encoding="UTF-8", xml_declaration=True, with_tail=True
        )


if __name__ == "__main__":
    now = datetime.now()
    today = f"{now.year}-{now.month:02d}-{now.day:02d}"

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "proceedings", nargs="+", help="List of paths to ACLPUB proceedings/ directories."
    )
    parser.add_argument(
        "--ingest-date",
        "-d",
        type=str,
        default=today,
        help="Ingestion date as YYYY-MM-DD. Default: %(default)s.",
    )
    anthology_path = os.path.join(os.path.dirname(sys.argv[0]), "..")
    parser.add_argument(
        "--anthology-dir",
        "-r",
        default=anthology_path,
        help="Root path of ACL Anthology Github repo. Default: %(default)s.",
    )
    pdfs_path = os.path.join(os.environ["HOME"], "anthology-files", "pdf")
    parser.add_argument(
        "--pdfs-dir", "-p", default=pdfs_path, help="Root path for placement of PDF files"
    )
    attachments_path = os.path.join(os.environ["HOME"], "anthology-files", "attachments")
    parser.add_argument(
        "--attachments-dir",
        "-a",
        default=attachments_path,
        help="Root path for placement of PDF files",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Don't actually copy anything."
    )
    args = parser.parse_args()

    main(args)
