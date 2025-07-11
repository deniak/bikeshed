from __future__ import annotations

import copy
import dataclasses
import re
from collections import defaultdict

from .. import config, constants, retrieve, t
from .. import messages as m
from . import utils, wrapper
from .utils import LinkFailure

LAZY_LOADED_SOURCES: list[str] = ["foreign"]


class RefSource:
    __slots__ = [
        "_loadedAnchorGroups",
        "dataFile",
        "fors",
        "ignoredSpecs",
        "methods",
        "refs",
        "replacedSpecs",
        "source",
        "specs",
    ]

    # Which sources use lazy-loading; other sources always have all their refs loaded immediately.

    def __init__(
        self,
        source: str,
        specs: dict[str, dict[str, str]] | None = None,
        ignored: set[str] | None = None,
        replaced: set[tuple[str, str]] | None = None,
        fileRequester: t.DataFileRequester | None = None,
    ) -> None:
        if fileRequester is None:
            self.dataFile = retrieve.defaultRequester
        else:
            self.dataFile = fileRequester

        # String identifying which refsource this is.
        self.source = source

        # Dict of {linking text => [anchor data]}
        self.refs: defaultdict[str, list[t.RefWrapper]] = defaultdict(list)

        # Dict of {argless method signatures => {"argfull signature": {"args":[args], "for":[fors]}}}
        self.methods: dict[str, MethodVariants] = {}

        # Dict of {for value => [terms]}
        self.fors: defaultdict[str, list[str]] = defaultdict(list)

        self.specs: dict[str, dict[str, str]] = {} if specs is None else specs
        self.ignoredSpecs = set() if ignored is None else ignored
        self.replacedSpecs = set() if replaced is None else replaced
        self._loadedAnchorGroups: set[str] = set()

    def fetchRefs(self, key: str) -> list[t.RefWrapper]:
        """Safe, lazy-loading version of self.refs[key]"""

        if key in self.refs:
            return self.refs[key]

        if self.source not in LAZY_LOADED_SOURCES:
            return []

        group = config.groupFromKey(key)
        if group in self._loadedAnchorGroups:
            # Group was loaded, but previous check didn't find it, so it's just not here.
            return []
        # Otherwise, load the group file.
        with self.dataFile.fetch("anchors", f"anchors-{group}.data", okayToFail=True) as fh:
            self.refs.update(decodeAnchors(fh))
            self._loadedAnchorGroups.add(group)
        return self.refs.get(key, [])

    def fetchAllRefs(self) -> list[tuple[str, list[t.RefWrapper]]]:
        """Nuts to lazy-loading, just load everything at once."""

        if self.source not in LAZY_LOADED_SOURCES:
            return list(self.refs.items())

        for file in self.dataFile.walkFiles("anchors"):
            group = t.cast(re.Match, re.match(r"anchors-(.{2})", file)).group(1)
            if group in self._loadedAnchorGroups:
                # Already loaded
                continue
            with self.dataFile.fetch("anchors", file) as fh:
                self.refs.update(decodeAnchors(fh))
                self._loadedAnchorGroups.add(group)
        return list(self.refs.items())

    def queryRefs(
        self,
        text: str | None = None,
        spec: str | None = None,
        linkType: str | None = None,
        linkFor: str | list[str] | None = None,
        explicitFor: bool = False,
        linkForHint: str | None = None,
        status: str | None = None,
        statusHint: str | None = None,
        export: bool | None = None,
        ignoreObsoletes: bool = False,
        latestOnly: bool = True,
        dedupURLs: bool = True,
        exact: bool | None = None,
        error: bool = False,
        el: t.ElementT | None = None,
    ) -> tuple[list[t.RefWrapper], LinkFailure | None, list[t.RefWrapper]]:
        if exact is not None:
            return self._queryRefs(
                text=text,
                spec=spec,
                linkType=linkType,
                linkFor=linkFor,
                explicitFor=explicitFor,
                linkForHint=linkForHint,
                status=status,
                statusHint=statusHint,
                export=export,
                ignoreObsoletes=ignoreObsoletes,
                latestOnly=latestOnly,
                dedupURLs=dedupURLs,
                error=error,
                el=el,
                exact=exact,
            )
        else:
            # First search for the exact term, and only if it fails fall back to conjugating.
            results, linkFailure, _ = self._queryRefs(
                text=text,
                spec=spec,
                linkType=linkType,
                linkFor=linkFor,
                explicitFor=explicitFor,
                linkForHint=linkForHint,
                status=status,
                statusHint=statusHint,
                export=export,
                ignoreObsoletes=ignoreObsoletes,
                latestOnly=latestOnly,
                dedupURLs=dedupURLs,
                error=error,
                el=el,
                exact=True,
            )
            if linkFailure:
                return self._queryRefs(
                    text=text,
                    spec=spec,
                    linkType=linkType,
                    linkFor=linkFor,
                    explicitFor=explicitFor,
                    linkForHint=linkForHint,
                    status=status,
                    statusHint=statusHint,
                    export=export,
                    ignoreObsoletes=ignoreObsoletes,
                    latestOnly=latestOnly,
                    dedupURLs=dedupURLs,
                    error=error,
                    el=el,
                    exact=False,
                )
            else:
                return results, None, []

    def allRefsIterator(self) -> t.Generator[t.RefWrapper, None, None]:
        # Turns a dict of arrays of refs into an iterator of refs
        for _, group in self.fetchAllRefs():
            yield from group

    def textRefsIterator(self, texts: list[str]) -> t.Generator[t.RefWrapper, None, None]:
        # Same as above, but only grabs those keyed to a given text
        for x in texts:
            yield from self.fetchRefs(x)

    def forRefsIterator(self, targetFors: str | list[str]) -> t.Generator[t.RefWrapper, None, None]:
        # Same as above, but only grabs those for certain values
        if isinstance(targetFors, str):
            targetFors = [targetFors]
        for for_ in targetFors:
            for x in self.fors[for_]:
                yield from self.fetchRefs(x)

    def _queryRefs(
        self,
        text: str | None = None,
        spec: str | None = None,
        linkType: str | None = None,
        linkFor: str | list[str] | None = None,
        explicitFor: bool = False,
        linkForHint: str | None = None,
        status: str | None = None,
        statusHint: str | None = None,
        export: bool | None = None,
        ignoreObsoletes: bool = False,
        latestOnly: bool = True,
        dedupURLs: bool = True,
        exact: bool = False,
        error: bool = False,
        el: t.ElementT | None = None,
    ) -> tuple[list[t.RefWrapper], LinkFailure | None, list[t.RefWrapper]]:
        # Query the ref database.
        # If it fails to find a ref, also returns the stage at which it finally ran out of possibilities.

        # Set up the initial list of refs to query
        if text:
            if exact:
                refs = list(self.textRefsIterator([text]))
            else:
                textsToSearch = list(utils.linkTextVariations(text, linkType))

                if any(st.endswith("()") for st in textsToSearch):
                    # Let argless methods (either with () at the end, or no parens at all)
                    # pull in their argful variants for searching.
                    for st in textsToSearch[:]:
                        textsToSearch += getArgfulMethodVariants(st, self)

                if (linkType is None or linkType in config.lowercaseTypes) and text.lower() != text:
                    textsToSearch += [t.lower() for t in textsToSearch]
                refs = list(self.textRefsIterator(textsToSearch))
        elif linkFor:
            refs = list(self.forRefsIterator(linkFor))
        else:
            refs = list(self.allRefsIterator())
        if not refs:
            return [], LinkFailure.Text, []

        oldRefs = refs
        if linkType:
            if linkType in config.dfnTypes:
                linkTypes = [linkType]
            elif linkType in config.linkTypeToDfnType:
                linkTypes = list(config.linkTypeToDfnType[linkType])
            else:
                if error:
                    m.linkerror(f"Unknown link type '{linkType}'.", el=el)
                return [], LinkFailure.Type, []
            refs = [x for x in refs if x.type in linkTypes]
        if not refs:
            return [], LinkFailure.Type, oldRefs

        oldRefs = refs
        if export is not None:
            refs = [x for x in refs if x.export == export]
        if not refs:
            return [], LinkFailure.Export, oldRefs

        oldRefs = refs
        if spec:
            refs = [x for x in refs if spec in (x.spec, x.shortname)]
        if not refs:
            return [], LinkFailure.Spec, oldRefs

        """
        for=A | forHint=B | explicitFor
        ✘ | ✘ | ✘ = anything
        ✘ | ✘ | ✔ = /
        ✘ | ✔ | ✘ = B/, fallback to anything
        ✘ | ✔ | ✔ = B/, fallback to /
        ✔ | ✘ | ✘ = A/
        ✔ | ✘ | ✔ = A/
        ✔ | ✔ | ✘ = A/
        ✔ | ✔ | ✔ = A/
        """

        oldRefs = refs
        if linkFor:
            refs = filterByFor(refs, linkFor)
        elif linkForHint:
            if explicitFor:
                tempRefs = filterByFor(refs, linkForHint)
                if not tempRefs:
                    tempRefs = filterByFor(refs, "/")
                refs = tempRefs
            else:
                # Handled later, in the "just a hint" section.
                pass
        elif explicitFor:
            refs = filterByFor(refs, "/")
        if not refs:
            return [], LinkFailure.For, oldRefs

        oldRefs = refs
        if status:
            refs = filterByStatus(self, refs, status)
        if not refs:
            return [], LinkFailure.Status, refs

        if status is None and statusHint:
            tempRefs = filterByStatus(self, refs, statusHint)
            if tempRefs:
                refs = tempRefs

        if linkForHint and not linkFor and not explicitFor:
            tempRefs = filterByFor(refs, linkForHint)
            if tempRefs:
                refs = tempRefs

        oldRefs = refs
        if ignoreObsoletes and not spec:
            # Remove any ignored or obsoleted specs
            # If you specified the spec, don't filter things - you know what you're doing.
            refs = utils.filterObsoletes(refs, replacedSpecs=self.replacedSpecs, ignoredSpecs=self.ignoredSpecs)
        if not refs:
            return [], LinkFailure.IgnoredSpecs, oldRefs

        if dedupURLs:
            # With non-exact texts, you might have multiple "anchors"
            # that point to the same url. Dedup them.
            seenUrls = set()
            tempRefs = []
            # Sort the refs so the kept one doesn't depend on ordering in the RefSource dict.
            for ref in sorted(copy.copy(refs), key=lambda x: x.text):
                if ref.url not in seenUrls:
                    tempRefs.append(ref)
                    seenUrls.add(ref.url)
            refs = tempRefs

        if latestOnly:
            # If multiple levels of the same shortname exist,
            # only use the latest level.
            # If generating for a snapshot, prefer the latest snapshot level,
            # unless that doesn't exist, in which case just prefer the latest level.
            refs = utils.filterOldVersions(refs, status=status or statusHint)

        return refs, None, []

    def addMethodVariants(self, methodSig: str, forVals: t.Iterable[str], shortname: str | None) -> None:
        # Takes a full method signature, like "foo(bar)",
        # and adds appropriate lines to self.methods for it
        match = re.match(r"([^(]+)\((.*)\)", methodSig)
        if not match:
            # Was fed something that's not a method signature.
            return None
        name, args = match.groups()
        arglessMethodSig = name + "()"
        if arglessMethodSig not in self.methods:
            self.methods[arglessMethodSig] = MethodVariants(arglessMethodSig, {})
        variants = self.methods[arglessMethodSig].variants
        if methodSig not in variants:
            args = [x.strip() for x in args.split(",")]
            variants[methodSig] = MethodVariant(signature=methodSig, args=args, for_=[], shortname=shortname)
        variants[methodSig].for_.extend(forVals)


def filterByFor(refs: t.Sequence[t.RefWrapper], linkFor: str | list[str]) -> list[t.RefWrapper]:
    return [x for x in refs if matchFor(x.for_, linkFor)]


def matchFor(forVals: list[str], forTest: str | list[str]) -> bool:
    # forTest can be a string, either / for no for or the for value to match,
    # or an array of strings, of which any can match
    if forTest == "/":
        return not bool(forVals)
    if isinstance(forTest, str):
        return forTest in forVals
    return any(matchFor(forVals, ft) for ft in forTest)


def filterByStatus(source: RefSource, refs: t.Sequence[t.RefWrapper], status: str) -> list[t.RefWrapper]:
    if status in constants.refStatus:
        # If status is "current'", kill snapshot refs unless their spec *only* has a snapshot_url
        if status == constants.refStatus.current:
            return [
                ref
                for ref in refs
                if ref.status == "current"
                or (ref.status == "snapshot" and source.specs.get(ref.spec, {}).get("current_url") is None)
            ]
        # If status is "snapshot", kill current refs if there's a corresponding snapshot ref for the same spec.
        elif status == constants.refStatus.snapshot:
            snapshotSpecs = [ref.spec for ref in refs if ref.status == "snapshot"]
            return [
                ref
                for ref in refs
                if ref.status == "snapshot" or (ref.status == "current" and ref.spec not in snapshotSpecs)
            ]
    # Status is a non-refStatus, but is a valid linkStatus, like "local"
    elif status in config.linkStatuses:
        return [x for x in refs if x.status == status]

    msg = f"Status value of '{status}' not handled"
    raise Exception(msg)


def decodeAnchors(linesIter: t.Iterator[str]) -> defaultdict[str, list[t.RefWrapper]]:
    # Decodes the anchor storage format into {key: [{anchor-data}]}
    anchors = defaultdict(list)
    try:
        while True:
            aText = next(linesIter)[:-1]
            displayText = next(linesIter)[:-1]
            data: wrapper.RefDataT = {
                "type": next(linesIter),
                "spec": next(linesIter),
                "shortname": next(linesIter),
                "level": next(linesIter),
                "status": next(linesIter),
                "url": next(linesIter),
                "export": next(linesIter) != "\n",
                "normative": next(linesIter) != "\n",
                "for_": [],
            }
            while True:
                line = next(linesIter)
                if line == "-\n":
                    break
                t.cast("list", data["for_"]).append(line)
            anchors[aText].append(wrapper.RefWrapper(aText, displayText, data))
    except StopIteration:
        return anchors


def getArgfulMethodVariants(maybeMethodSig: str, refs: RefSource) -> list[str]:
    if maybeMethodSig.endswith("()") and maybeMethodSig in refs.methods:
        return list(refs.methods[maybeMethodSig].variants.keys())
    return []


@dataclasses.dataclass
class MethodVariants:
    arglessSignature: str
    variants: dict[str, MethodVariant]


@dataclasses.dataclass
class MethodVariant:
    signature: str
    args: list[str]
    for_: list[str]
    shortname: str | None
