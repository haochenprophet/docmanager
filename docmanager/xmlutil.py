#
# Copyright (c) 2014-2015 SUSE Linux GmbH
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of version 3 of the GNU General Public License as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, contact SUSE LLC.
#
# To contact SUSE about this file by physical or electronic mail,
# you may find current contact information at www.suse.com

import copy
from collections import namedtuple
from docmanager.core import ReturnCodes
from io import StringIO
from itertools import accumulate
import re
import os
import sys

import xml.sax

# -------------------------------------------------------------------
# Regular Expressions

ENTS = re.compile("(&([\w_\.-]+);)")
STEN = re.compile("(\[\[\[(\#?[\w_\.-]+)\]\]\])")


def ent2txt(match, start="[[[", end="]]]"):
    """Replace any &text; -> [[[text]]]

    :param _sre.SRE_Match match: match object from re
    :param str start: Start string of entity replacement
    :param str end:   end string
    :return: replaced string
    :rtype: str
    """
    if match:
        return "{}{}{}".format(start,
                               match.group(2),
                               end)


def txt2ent(match):
    """Replace any [[[text]]] -> &text;

    :param _sre.SRE_Match match: match object from re
    :return: replaced string
    :rtype: str
    """
    if match:
        return "&{};".format(match.group(2))


def preserve_entities(text):
    """Preserve any entities in text

    :param str text: the text that should preserve entities
    :return: the preserved text
    :rtype: str
    """
    return ENTS.sub(ent2txt, text)


def recover_entities(text):
    """Recover any preserved entities in text

    :param str text: the text that should recover entities
    :return: the recovered text
    :rtype: str
    """
    return STEN.sub(txt2ent, text)


def replaceinstream(stream, func):
    """Preserve or restore any entities in a stream or file-like object
       depending on the function `func`

    :param stream: iterable stream or file-like object
    :param func: replacement function, signature: func(text)
    :return: another stream with replaced entities
    :rtype: StringIO
    """
    result = StringIO()

    for line in stream:
        result.write(func(line))

    result.seek(0)
    return result

# -------------------------------------------------------------------

def isXML(text):
    """Checks if a text starts with a typical XML construct

       :param str text: The text to observe
       :return: True, if text can be considered as XML, otherwise False
       :rtype: bool
    """
    possiblestartstrings = (re.compile("<\?xml"),
                            re.compile("<!DOCTYPE"),
                            re.compile("<!--",),
                            re.compile(r'<(?P<tag>(?:(?P<prefix>\w+):)?'
                                        '(?P<name>[a-zA-Z0-9_]+))\s*'),
                           )
    result = False
    for matcher in possiblestartstrings:
        match = matcher.search(text)
        if match:
            result = True
            break
    return result


def ensurefileobj(source):
    """Return a file(-like) object, regardless if it's a another
       file-object, a filename, or a string

       :param source: filename, file-like object, or string
       :return: StringIO or file-like object
    """

    # StringIO support:
    if hasattr(source, 'getvalue') and hasattr(source, 'tell'):
        # we return the source
        return source
    elif isinstance(source, (str, bytes)):
        if isXML(source):
            return StringIO(source)
        else:
            # source isn't a file-like object nor starts with XML structure
            # so it has to be a filename
            return StringIO(open(source, 'r').read())
    # TODO: Check if source is an URL; should we allow this?


# -------------------------------------------------------------------
# Helper functions

def localname(tag):
    """Returns the local name of an element

    :param str tag: Usually in the form of {http://docbook.org/ns/docbook}article
    :return:  local name
    :rtype:  str
    """
    m = re.search("\{(?P<ns>.*)\}(?P<local>[\w]+)", tag)
    if m:
        return m.groupdict()['local']
    else:
        return tag


def compilestarttag(roottag=None):
    """Compile a regular expression for start tags like <article> or
       <d:book> with or without any  attributes

       :param str roottag: Name of roottag or None, for a general tag
       :return: a pattern object
       :rtype: _sre.SRE_Pattern
    """
# Taken from the xmllib.py
# http://code.metager.de/source/xref/python/jython/lib-python/2.7/xmllib.py
    _S = '[ \t\r\n]+'                       # white space
    _opS = '[ \t\r\n]*'                     # optional white space
    _Name = '[a-zA-Z_:][-a-zA-Z0-9._:]*'    # valid XML name
    _QStr = "(?:'[^']*'|\"[^\"]*\")"        # quoted XML string
    attrfind = re.compile(
        _S + '(?P<name>' + _Name + ')'
        '(' + _opS + '=' + _opS +
        '(?P<value>' + _QStr + '|[-a-zA-Z0-9.:+*%?!\(\)_#=~]+))?')
    starttagend = re.compile(_opS + '(?P<slash>/?)>')
    if roottag:
        root = '<(?P<tagname>' + roottag + ')'
    else:
        root = '<(?P<tagname>' + _Name + ')'
    return re.compile(root + '(?P<attrs>(?:' + attrfind.pattern + ')*)' +
                      starttagend.pattern)


# -------------

class LocatingWrapper(object):
    """Holds a table which are used to transform line and column position
       into offset
    """
    def __init__(self, f):
        self.f = f
        self.offset = [0]
        self.curoffs = 0

    def read(self, *a):
        data = self.f.read(*a)
        self.offset.extend(accumulate(len(m)+1 for m in data.split('\n')))
        return data

    def where(self, loc):
        return self.offset[loc.getLineNumber() - 1] + loc.getColumnNumber()

    def close(self):
        # Normally, we would close our file(-alike) object and call
        #   self.f.close()
        # However, we need this object later, so do nothing
        pass


class Handler(xml.sax.handler.ContentHandler):
    """ContentHandler to watch for start and end elements. Needed to
       get the location of all the elements
    """
    def __init__( self, context, locator):
        # handler.ContentHandler.__init__( self )
        super().__init__()
        self.context = context
        self.locstm = locator
        self.pos = namedtuple('Position', ['line', 'col', 'offset'])

    def setDocumentLocator(self, loc):
        self.loc = loc

    def comment(self, text):
        """XML Comment"""
        ctxlen = len(self.context)
        # We are only interested in the first two start tags
        if ctxlen:
            current = self.locstm.where(self.loc)
            pos = self.pos(self.loc.getLineNumber(), \
                            self.loc.getColumnNumber(), \
                            current)
            self.context.append(["-- comment", pos])

    def startCDATA(self):
        pass
    endCDATA = startCDATA

    def startDTD(self,  doctype, publicID, systemID):
        pass
    def endDTD(self):
        pass
    def startEntity(self, name):
        pass

    def startElement(self, name, attrs):
        ctxlen = len(self.context)
        # We are only interested in the first two start tags
        if ctxlen < 2:
            current = self.locstm.where(self.loc)
            pos = self.pos(self.loc.getLineNumber(), \
                         self.loc.getColumnNumber(), \
                         current)
            self.context.append(["%s" % name, pos])

    def endElement(self, name):
        eline = self.loc.getLineNumber()
        ecol = self.loc.getColumnNumber()
        last = self.locstm.where(self.loc)
        pos = self.pos(line=eline, col=ecol, offset=last)

        # save the position of an end tag and add '/' in front of the
        # name to distinguish it from a start tag
        self.context.append(["/%s" % name, pos])

    def processingInstruction(self, target, data):
        ctxlen = len(self.context)
        # Only append PIs when it's NOT before start-tag
        if ctxlen:
            current = self.locstm.where(self.loc)
            pos = self.pos(self.loc.getLineNumber(), \
                            self.loc.getColumnNumber(), \
                            current)
            self.context.append(["?%s" % target, pos])


def findprolog(source, maxsize=5000):
    """Returns a dictionary with essential information about the prolog

    :param source:
    :type source: source, file object, or file-like object
                  expected to be well-formed
    :param int maxize: Maximum size of bytes to read into XML buffer
    :return: { 'header': '...', # str everything before the start tag
               'root':   '...', # str: start tag from '<' til '>'
               'offset:  1,     # Integer
             }
    :rtype: dict
    """
    result = {}

    # context is used to save our locations
    context = []

    buf = ensurefileobj(source)
    # We read in maxsize and hope this is enough...
    xmlbuf = buf.read(maxsize)
    buf.seek(0)
    locstm = LocatingWrapper(buf)
    parser = xml.sax.make_parser()

    # Disable certain features:
    # no validation, no external general and parameter entities
    parser.setFeature(xml.sax.handler.feature_validation, False)
    parser.setFeature(xml.sax.handler.feature_external_ges, False)
    parser.setFeature(xml.sax.handler.feature_external_pes, False)

    handler = Handler(context, locstm)
    parser.setProperty(xml.sax.handler.property_lexical_handler, handler);

    parser.setContentHandler(handler)
    parser.parse(locstm)

    first = context[0]
    soffset = first[1].offset
    doctype = xmlbuf[:soffset]

    # Check if we have reached the "end tag" (symbolized with '/' in
    # its first character).
    # If yes, start and end tag is on the same line and we can use the
    # last entry.
    # If not, we need to look in the next entry
    if context[1][0][0] == '/':
        last = context[-1]
    elif context[1][0][0] ==  '-':
        last = context[1]
    else:
        last = context[1]

    eoffset = last[1].offset
    starttag = xmlbuf[soffset:eoffset].rstrip(' ')

    result['header'] = doctype
    result['root'] = starttag
    result['offset'] = len(doctype)
    result['roottag'] = context[0][0]
    return result
