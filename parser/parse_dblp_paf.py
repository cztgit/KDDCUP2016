'''
Created on Mar 28, 2016

@author: hugo
An event-driven parser which parses paper-author-affil relationship
'''

import requests
import xml.sax
import xml.dom.minidom
from mymysql.mymysql import MyMySQL
from datasets.affil_names import url_keywords
import config
import subprocess
import sys
import re

total_lineno = None
BASE_URL = 'http://dblp.uni-trier.de/'

db = MyMySQL(config.DB_NAME, user=config.DB_USER, passwd=config.DB_PASSWD)
auth_affil_bulk = set()


url_pattern = '^(http[s]*://)([.a-z^/][^/]+[.a-z^/])?'
url_prog = re.compile(url_pattern)
# alltags = set()

# All tags we have in dblp.xml
# [u'www', u'isbn', u'ee', u'series', u'number', u'month', u'mastersthesis', u'year', u'sub', u'title', u'incollection', u'booktitle', u'note', u'book', u'editor', u'sup', u'cite', u'journal', u'volume', u'address', u'cdrom', u'article', u'pages', u'crossref', u'chapter', u'publisher', u'school', u'phdthesis', u'dblp', u'inproceedings', u'i', u'author', u'url', u'proceedings', u'tt']
class DBLPHandler(xml.sax.ContentHandler):
    def __init__(self, locator, table_name, fields):
        self.loc = locator
        self.setDocumentLocator(self.loc)
        self.CurrentData = ""
        self.valid = False # check if www tag
        self.is_affil = False # check if valid (having affiliation attr) note tag
        self.url = "" # we use urls to retrieve affiliations
        self.dblp_key = "" # dblp key for each author
        self.authors = set()
        self.affils = set()
        # self.auth_affil_bulk = set()
        self.valid_count = 0 # num of homepage records
        self.author_count = 0 # num of valid (i.e., having affils) authors
        self.count = 0 # num of tags

    # # Weird errors, does not work, so we adopt a stupid way here, set auth_affil_bulk as global
    # def __del__(self):
    #     super(xml.sax.ContentHandler, self).__del__()
    #     if self.auth_affil_bulk:
    #         db.insert(into=self.table_name, fields=self.fields, values=self.auth_affil_bulk, ignore=True)


    # Call when an element starts
    def startElement(self, tag, attr):
        self.count += 1
        if self.count % 1000000 == 0:
            print "progress: %.2f %%" % (self.get_progress()*100)

        self.CurrentTag = tag
        if tag == "www" and attr.has_key("key") \
                and "homepages" in attr["key"].split("/"):
            # www homepage tage
            self.valid = True
            self.valid_count += 1
            self.dblp_key = attr["key"]

        elif self.valid and tag == "note" and attr.has_key("type") \
                and attr["type"] == "affiliation":
            # affiliation
            self.is_affil = True
        if tag == 'dblpperson':
            print attr
            import pdb;pdb.set_trace()
        # alltags.add(tag)


    # Call when an elements ends
    def endElement(self, tag):
        if self.valid and tag == "www":

            self.valid = False # reset flag

            # pack data
            if not self.affils and self.url: # retrieve affils based on urls
                self.affils = retrieve_affils_by_urls(self.url)
                if not self.affils:
                    print self.url#, self.affils


            if self.affils:
                affil_names = self.affils

                if self.authors:
                    author_name = list(self.authors)[0]
                    pubs = get_pubs_by_authors(self.dblp_key, author_name) # passes author_name before cleanning it
                    author_name = re.sub(" \d+", " ", author_name) # remove digits at the end of the string
                    other_names = list(self.authors)[1:]
                    # write to db
                    # print "author name: %s" % author_name
                    # print "other names: %s" % other_names
                    # print "affil names: %s" % affil_name
                    # print

                    # one author may have multiple affils, we store all of them
                    auth_affils = set()
                    for each in affil_names:
                        auth_affils.add((author_name, '/'.join([x for x in other_names]), each))

                    auth_affil_bulk.update(auth_affils)

                    if len(auth_affil_bulk) % 500 == 0:
                        # db.insert(into=table_name, fields=fields, values=list(auth_affil_bulk), ignore=True)
                        auth_affil_bulk.clear()

                    self.author_count += 1
                    if self.author_count % 1000 == 0:
                        print "%s valid (having affils) authors processed." % self.author_count


            if self.authors:
                self.authors.clear()
            if self.affils:
                self.affils.clear()
            self.url = ""

            if self.valid_count % 1000 == 0:
                print "%s homepages processed."%self.valid_count


        elif self.is_affil and tag == "note":
            self.is_affil = False

        # elif self.CurrentTag == "author":
        #     print "Author:", self.author
        # elif self.CurrentTag == "note":
        #     print "Note:", self.note
        # elif self.CurrentTag == "title":
        #     print "Title:", self.title
        # elif self.CurrentTag == "url":
        #     print "URL:", self.url

        self.CurrentTag = ""

    # Call when a character is read
    def characters(self, content):
        if self.valid and self.CurrentTag == "author":
            self.authors.add(content.strip('\r\n').strip())

        elif self.is_affil and self.CurrentTag == "note":
            self.affils.add(content.strip('\r\n').strip())

        elif self.valid and self.CurrentTag == "url":
            self.url = content.strip()
        # elif self.valid and self.CurrentTag == "title":
            # self.title = content

    def get_progress(self):
        global total_lineno
        return self.loc.getLineNumber()/float(total_lineno)


# get total line num of a file
def file_len(fname):
    p = subprocess.Popen(['wc', '-l', fname], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE)
    result, err = p.communicate()
    if p.returncode != 0:
        raise IOError(err)
    return int(result.strip().split()[0])


def retrieve_affils_by_urls(url):
    rst = url_prog.search(url)
    if rst:
        url_tokens = rst.group(0).replace('/', '.').split('.')
        for k, v in url_keywords.iteritems():
            if k in url_tokens:
                return set([v])

    return set()



# We cannot parse author_names with special characters currently
def get_pubs_by_authors(author_name, dblp_key):
    if not author_name:
        return set()

    # generate the url linked to the author's homepage
    rst = re.search(" \d+", author_name.strip()) # e.g., "Chen Li 0001"
    suffix = rst.group(0) if rst else ''

    name_token = author_name.replace(suffix,'').split(' ')

    last_name = ("%s%s"%(name_token[-1], suffix)).replace(' ', '_')
    # print last_name
    # e.g. "Chen Li 0001" -> "Li_0001:Chen", "Li-Chieh Chen" -> "Chen:Chieh=Li"
    # "Mohammed J. Zaki" -> "Zaki:Mohammed_J=", "Yunhua Li" -> "Li:Yunhua"
    # replace ' ' with '_', ('.', '-', ''') with '='
    url_key = (last_name + ':' + ('_'.join(name_token[:-1]))).replace('.', '=').replace('-', '=').replace("'", '=')
    # print url_key
    url = "%spers/xx/%s/%s.xml"%(BASE_URL, last_name[0].lower(), url_key)
    # print url

    # fetchs and parses the xml file
    resp = requests.get(url)
    if resp.status_code == 200:
        # import pdb;pdb.set_trace()
        DOMTree = xml.dom.minidom.parseString(resp.content)
        dblp_person = DOMTree.documentElement
        # checks if this is exactlly the wanted homepage using dblp key
        person = dblp_person.getElementsByTagName("person")
        if person and person[0].hasAttribute("key"):
            if not dblp_key == person[0].getAttribute("key"): # not the right person
                print "failed to find author: %s" % author_name
                import pdb;pdb.set_trace()
                return set()

            else:
                # right person
                # fetchs pubs
                docs_set = set()
                # fetchs conference pubs
                pubs = dblp_person.getElementsByTagName("r")
                for each_pub in pubs:
                    doc = each_pub.getElementsByTagName("inproceedings") # conference papers
                    if doc:
                        title = doc[0].getElementsByTagName("title")
                        if title:
                            docs_set.add(title[0].childNodes[0].data.strip('. '))
                return docs_set
        else:
            print "failed to find author: %s" % author_name
            import pdb;pdb.set_trace()
            return set()

        dblp.getElementsByTagName("r")
    else:
        print "failed to find author: %s" % author_name
        import pdb;pdb.set_trace()
        return set()


if __name__ == "__main__":
    try:
        in_file = sys.argv[1]
    except Exception, e:
        print e
        sys.exit()

    # total_lineno = file_len(in_file)
    # print "total line num of the file: %s\n" % total_lineno

    # # db info
    # # table 1)
    # table_description_auth_affil = ['id INT NOT NULL AUTO_INCREMENT',
    #                     'dblp_key VARCHAR(200) NOT NULL',
    #                     'name VARCHAR(200) NOT NULL',
    #                     'other_names VARCHAR(1000)',
    #                     'affil_name VARCHAR(200)',
    #                     'PRIMARY KEY (id)',
    #                     'KEY (dblp_key)',
    #                     'KEY (name)']

    # table_auth_affil = "dblp_auth_affil"
    # fields_auth_affil = ["dblp_key", "name", "other_names", "affil_name"]

    # # table 2)
    # table_description_auth_pub = ['id INT NOT NULL AUTO_INCREMENT',
    #                     'dblp_key VARCHAR(200) NOT NULL',
    #                     'pub_title VARCHAR(300) NOT NULL'
    #                     'PRIMARY KEY (id)',
    #                     'KEY (dblp_key)']

    # table_auth_pub = "dblp_auth_pub"
    # fields_auth_pub = ["dblp_key", "pub_title"]

    # # create table
    # db.create_table(table_auth_affil, table_description_auth_affil, force=True)
    # db.create_table(table_auth_pub, table_description_auth_pub, force=True)


    # # create an XMLReader
    # parser = xml.sax.make_parser()
    # locator = xml.sax.expatreader.ExpatLocator(parser)
    # # turn off namepsaces
    # parser.setFeature(xml.sax.handler.feature_namespaces, 0)
    # # override the default ContextHandler
    # Handler = DBLPHandler(locator, table_name, fields)
    # parser.setContentHandler(Handler)
    # parser.parse(in_file)

    # # write remaining data into db
    # if auth_affil_bulk:
    #     # db.insert(into=table_auth_affil, fields=fields_auth_affil, values=list(auth_affil_bulk), ignore=True)
    #     # db.insert(into=table_auth_pub, fields=fields_auth_pub, values=list(auth_affil_bulk), ignore=True)
    #     pass

    # print len(alltags)

    docs_set = get_pubs_by_authors(in_file, sys.argv[2])
    print docs_set
    print len(docs_set)
    print "It's done."
