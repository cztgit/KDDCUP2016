"""
Created on Mar 14, 2016

@author: hugo
"""

import chardet
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import networkx as nx
from mymysql import MyMySQL
from collections import defaultdict, OrderedDict
from exceptions import TypeError

# from pylucene import Index
import itertools
import os
import sys
import logging as log
import words
import config
import utils
from datasets.mag import get_selected_docs, get_selected_expand_pubs, get_conf_docs, retrieve_affils_by_authors
from ranking.kddcup_ranker import rank_single_layer_nodes
import json
from copy import deepcopy

old_settings = np.seterr(all='warn', over='raise')


# Database connection
db = MyMySQL(db=config.DB_NAME,
             user=config.DB_USER,
             passwd=config.DB_PASSWD)


def get_all_edges(papers):
  """
  Retrieve all edges related to given papers from the database.
  """
  if hasattr(papers, '__iter__'):
    if len(papers) == 0:
      return []
    else:
      paper_str = ",".join(["'%s'" % paper_id for paper_id in papers])
  else:
      raise TypeError("Parameter 'papers' is of unsupported type. Iterable needed.")

  rows = db.select(fields=["paper_id", "paper_ref_id"], table="paper_refs",
            where="paper_id IN (%s) OR paper_ref_id IN (%s)"%(paper_str, paper_str))

  return rows


def show_stats(graph):
  print "%d nodes and %d edges." % (graph.number_of_nodes(), graph.number_of_edges())


def write_graph(graph, outfile):
  """
  Write the networkx graph into a file in the gexf format.
  """
  log.info("Dumping graph: %d nodes and %d edges." % (graph.number_of_nodes(), graph.number_of_edges()))
  nx.write_gexf(graph, outfile, encoding="utf-8")


def get_paper_year(paper_id):
  """
  Returns the year of the given paper as stored in the DB.
  """
  year = db.select_one(fields="year", table="papers", where="id='%s'" % paper_id)
  return (int(year) if year else 0)


# def show(doc_id):
#   """ Small utility method to show a document on the browser. """
#   from subprocess import call, PIPE

#   call(["google-chrome", "--incognito", "/data/pdf/%s.pdf" % doc_id], stdout=PIPE, stderr=PIPE)


def add_attributes(graph, entities, node_ids, atts):
  """
  Adds attributes to the nodes associated to the given entities (papers, authors, etc.)
  """
  for entity in entities:
    graph.add_node(node_ids[entity], **atts[entity])


def normalize_edges(edges):
  """
  Normalize the weight on given edges dividing by the maximum weight found.
  """
  wmax = 0.0
  for _u, _v, w in edges:
    wmax = max(w, wmax)

  return [(u, v, w / float(wmax)) for u, v, w in edges]


# def similarity(d1, d2):
#   """
#   Cosine similarity between sparse vectors represented as dictionaries.
#   """
#   sim = 0.0
#   for k in d1:
#     if k in d2:
#       sim += d1[k] * d2[k]

#   dem = np.sqrt(np.square(d1.values()).sum()) * np.sqrt(np.square(d2.values()).sum())
#   return sim / dem


def sorted_tuple(a, b):
  """ Simple pair sorting to avoid repetitions when inserting into set or dict. """
  return (a, b) if a < b else (b, a)


def get_rules_by_lift(transactions, min_lift=1.0):
  """
  Get strong rules from transactions and minimum lift provided.
  """
  freqs1 = defaultdict(int)  # Frequencies of 1-itemsets
  freqs2 = defaultdict(int)  # Frequencies of 2-itemsets
  for trans in transactions:
    for i in trans:
      freqs1[i] += 1

    # If there are at least 2 items, let's compute pairs support
    if len(trans) >= 2:
      for i1, i2 in itertools.combinations(trans, 2):
        freqs2[sorted_tuple(i1, i2)] += 1

  n = float(len(transactions))

  # Check every co-occurring ngram
  rules = []
  for (i1, i2), f in freqs2.items():

    # Consider only the ones that appear more than once together,
    # otherwise lift values can be huge and not really significant
    if f >= 1:
      lift = f * n / (freqs1[i1] * freqs1[i2])

      # Include only values higher than min_lift
      if lift >= min_lift:
        rules.append((i1, i2, lift))

  return rules


########################################
## Class definitions
########################################


class GraphBuilder:
  """
  Graph structure designed to store edges and operate efficiently on some specific
  graph building and expanding operations.
  """

  def __init__(self, edges):

    self.citing = defaultdict(list)
    self.cited = defaultdict(list)

    for f, t in edges:
      f = str(f).strip('\r\n')
      t = str(t).strip('\r\n')
      self.citing[f].append(t)
      self.cited[t].append(f)


  def follow_nodes(self, nodes):
    """
    Return all nodes one edge away from the given nodes.
    """
    new_nodes = set()
    for n in nodes:
      new_nodes.update(self.citing[n])
      new_nodes.update(self.cited[n])

    return new_nodes


  def subgraph(self, nodes):
    """
    Return all edges between the given nodes.
    """
    # Make sure lookup is efficient
    nodes = set(nodes)

    new_edges = []
    for n in nodes:

      for cited in self.citing[n]:
        if (n != cited) and (cited in nodes):
          new_edges.append((n, cited))

      for citing in self.cited[n]:
        if (n != citing) and (citing in nodes):
          new_edges.append((citing, n))

    return set(new_edges)


class ModelBuilder:
  """
  Main class for building the graphical model. The layers are built separately in their
  corresponding methods. Every layer is cached in a folder defined by the main parameters.
  """

  def __init__(self, include_attributes=False):
    """
    Initializes structures and load data into memory, such as the text index and
    the citation graph.
    """
    # # Build text index if non-existing
    # if not os.path.exists(config.INDEX_PATH):
    #   indexer = Indexer()
    #   indexer.add_papers(config.INDEX_PATH, include_text=False)

    # # Load text index
    # self.index = Index(config.INDEX_PATH, similarity="tfidf")

    # Graph structure that allows fast access to nodes and edges
    # self.edges_lookup = GraphBuilder(get_all_edges())

    # If attributes should be fetched and included in the model for each type of node.
    # Should be true for visualization and false for pure relevance calculation.
    self.include_attributes = include_attributes
    self.pub_years = defaultdict()

    # Create a helper boolean to check if citation contexts are
    # going to be used (some datasets don't have it available)
    # self.use_contexts = (config.DATASET == 'csx')

    # Load vocabulary for the tokens in the citation contexts
    # if self.use_contexts:
    #   self.ctxs_vocab, self.nctx = words.read_vocab(config.CTXS_VOCAB_PATH)

    log.debug("ModelBuilder constructed.")


  def get_weights_file(self, edges):
    return [(u, v, 1.0) for (u, v) in edges]


  def get_pubs_layer(self, conf_name, year, n_hops, exclude_list=[], expanded_year=[], expand_conf_year=[], expand_method='conf'):
    """
    First documents are retrieved from pub records of a targeted conference.
    Then we follow n_hops from these nodes to have the first layer of the graph (papers).
    """

    # Fetches all document that have at least one of the terms
    pubs = get_selected_docs(conf_name, year)
    docs = zip(*pubs)[0]
    # add year
    self.pub_years = dict(pubs)


    if expand_method == 'n_hops':

      # Get doc ids as uni-dimensional list
      self.edges_lookup = GraphBuilder(get_all_edges(docs))
      nodes = set(docs)

      # Expand the docs set by reference
      nodes = self.get_expanded_pubs_by_nhops(nodes, self.edges_lookup, exclude_list, n_hops)


    elif expand_method == 'conf':

      # Expand the docs by getting more papers from the targeted conference
      # expanded_pubs = self.get_expanded_pubs_by_conf(conf_name, [2009, 2010])
      nodes = set(docs)

      expanded_pubs = self.get_expanded_pubs_by_conf2(conf_name, expanded_year)

      # add year
      for paper, year in expanded_pubs:
        self.pub_years[paper] = year

      # Remove documents from the exclude list and keep only processed ids
      if expanded_pubs:
        expanded_docs = set(zip(*expanded_pubs)[0]) - set(exclude_list)
        nodes.update(expanded_docs)



      # expand docs set by getting more papers accepted by related conferences
      for conf, years in expand_conf_year:
        conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf, limit=1)[0]
        expanded_pubs = self.get_expanded_pubs_by_conf2(conf, years)

        # add year
        for paper, year in expanded_pubs:
          self.pub_years[paper] = year

        # Remove documents from the exclude list and keep only processed ids
        if expanded_pubs:
          expanded_docs = set(zip(*expanded_pubs)[0]) - set(exclude_list)
          nodes.update(expanded_docs)


      self.edges_lookup = GraphBuilder(get_all_edges(nodes))

    else:
      raise ValueError("parameter expand_method should either be n_hops or conf.")


    # Get the edges between the given nodes and add a constant the weight for each
    edges = self.edges_lookup.subgraph(nodes)

    # Get edge weights according to textual similarity between
    # the query and the citation context
    weighted_edges = self.get_weights_file(edges)

    # To list to preserve element order
    nodes = list(nodes)

    # Save into cache for reusing
    #       cPickle.dump((nodes, edges, self.query_sims), open(cache_file, 'w'))

    return nodes, weighted_edges


  def get_expanded_pubs_by_conf(self, conf_name, year):
    # Expand the docs by getting more papers from the targeted conference
    conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf_name, limit=1)[0]
    expanded_pubs = get_conf_docs(conf_id, year)

    return expanded_pubs


  def get_expanded_pubs_by_conf2(self, conf_name, year):
    if not year:
      return []

    # Expand the docs by getting more papers from the targeted conference
    conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf_name, limit=1)[0]

    year_str = ",".join(["'%s'" % y for y in year])
    year_cond =  " AND year IN (%s)"%year_str if year_str else ''
    expanded_pubs = db.select(["paper_id", "year"], "expanded_conf_papers2", where="conf_id='%s'%s"%(conf_id, year_cond))

    return expanded_pubs


  def get_expanded_pubs_by_nhops(self, nodes, edges_lookup, exclude_list, n_hops):
    new_nodes = nodes

    # We hop h times including all the nodes from these hops
    for h in xrange(n_hops):
      new_nodes = edges_lookup.follow_nodes(new_nodes)

      # Remove documents from the exclude list and keep only processed ids
      new_nodes -= set(exclude_list)

      # Than add them to the current set
      nodes.update(new_nodes)

      log.debug("Hop %d: %d nodes." % (h + 1, len(nodes)))

    return nodes


  def get_paper_based_coauthorships(self, papers, weighted=True):
    """
    Return all the collaborationships between the given papers. Edges to authors not
    provided (according to given papers) are not included.
    """

    if hasattr(papers, '__iter__'):
      if len(papers) == 0:
        return []
      else:
        paper_str = ",".join(["'%s'" % paper_id for paper_id in papers])
    else:
      raise TypeError("Parameter 'papers' is of unsupported type. Iterable needed.")


    rows = db.select(["paper_id", "author_id"], "paper_author_affils", where="paper_id IN (%s)"%paper_str)

    rows = set(rows) # Removing duplicate records
    author_papers = defaultdict(set)
    for paper, author in rows:
      author_papers[author].add(paper)

    coauthorships = []
    authors = author_papers.keys()
    for i in xrange(len(authors) - 1):
      for j in xrange(i + 1, len(authors)):
        npapers = float(len(author_papers[authors[i]] & author_papers[authors[j]]))

        if npapers > 0:
          # Apply log transformation to smooth values and avoid outliers
          npapers = 1.0 + np.log(npapers) if weighted else 1.0

          coauthorships.append((authors[i], authors[j], npapers))

    return authors, rows, coauthorships


  def get_authors(self, doc_id):
    """
    Return the authors associated with the given paper, if available.
    """
    return db.select("author_id", table="paper_author_affils", where="paper_id='%s'" % doc_id)


  def get_cached_coauthorship_edges(self, authors):
    """
    Return all the collaboration edges between the given authors. Edges to authors not provided are
    not included.
    """
    # For efficient lookup
    authors = set(authors)

    edges = set()
    for author_id in authors:
      coauthors = db.select(["author1", "author2", "npapers"], "coauthorships",
                  where="author1=%d OR author2=%d" % (author_id, author_id))
      for a1, a2, npapers in coauthors:

        # Apply log transformation to smooth values and avoid outliers
        # crushing other values after normalization
        npapers = 1.0 + np.log(npapers)

        if (a1 in authors) and (a2 in authors):
          edge = (a1, a2, 1.0) if a1 < a2 else (a2, a1, 1.0)
          edges.add(edge)

    # Normalize by max value and return them as a list
    return normalize_edges(edges)


  def get_coauthorship_edges(self, authors):
    """
    Return all the collaboration edges between the given authors. Edges to authors not provided are
    not included.
    """
    # For efficient lookup
    authors = set(authors)

    edges = set()
    for author_id in authors:
      coauthorships = db.select_query("""SELECT b.author_id FROM authorships a, authorships b
                                         WHERE (a.author_id=%d) AND (b.author_id!=%d) AND a.paper_id=b.paper_id""" \
                      % (author_id, author_id))

      # Count coauthorshiped pubs
      coauthors = defaultdict(int)
      for (coauthor,) in coauthorships:
        if coauthor in authors:
          coauthors[(author_id, coauthor)] += 1

      for (a1, a2), npapers in coauthors.items():

        # Apply log transformation to smooth values and avoid outliers
        # crushing other values after normalization
        weight = 1.0 + np.log(npapers)

        if (a1 in authors) and (a2 in authors):
          edge = (a1, a2, weight) if a1 < a2 else (a2, a1, weight)
          edges.add(edge)

    # Normalize by max value and return them as a list
    return normalize_edges(edges)


  def get_authorship_edges(self, papers_authors):
    """
    Return authorship edges [(doc_id, author), ...]
    """
    edges = []
    for doc_id, authors in papers_authors.items():
      edges.extend([(doc_id, author, 1.0) for author in authors])

    return edges


  def get_authors_layer(self, papers, ign_cache=False):
    """
    Retrieve relevant authors from DB (author of at least one paper given as argument)
    and assemble co-authorship and authorship nodes and edges.
    """

    # Try to load from cache
    #       cache_file = "%s/authors.p" % self.cache_folder
    #       if (not ign_cache) and os.path.exists(cache_file) :
    #           return cPickle.load(open(cache_file, 'r'))

    authors, auth_edges, coauth_edges = self.get_paper_based_coauthorships(papers)

    # Save into cache for reuse
    #       cPickle.dump((all_authors, coauth_edges, auth_edges), open(cache_file, 'w'))

    return authors, coauth_edges, auth_edges


  def get_relevant_topics(self, doc_topics, ntop=None, above=None):
    """
    Get the most important topics for the given document by either:
      * Taking the 'ntop' values if 'ntop' id provided or
      * Taking all topics with contributions greater than 'above'.
    """
    if ntop:
      return np.argsort(doc_topics)[::-1][:ntop]

    if above:
      return np.where(doc_topics > above)[0]

    raise TypeError("Arguments 'ntop' and 'above' cannot be both None.")


  def get_frequent_topic_pairs(self, topics_per_document, min_interest):

    freqs1 = defaultdict(int)  # Frequencies of 1-itemsets
    freqs2 = defaultdict(int)  # Frequencies of 2-itemsets
    for topics in topics_per_document:
      for t in topics:
        freqs1[t] += 1

      if len(topics) >= 2:
        for t1, t2 in itertools.combinations(topics, 2):
          freqs2[sorted_tuple(t1, t2)] += 1

    total = float(len(topics_per_document))

    rules = []
    for (t1, t2), v in sorted(freqs2.items(), reverse=True, key=lambda (k, v): v):

      int12 = float(v) / freqs1[t1] - freqs1[t2] / total
      int21 = float(v) / freqs1[t2] - freqs1[t1] / total

      if int12 >= min_interest: rules.append((t1, t2, int12))
      if int21 >= min_interest: rules.append((t2, t1, int21))

    #   for interest, (t1, t2) in sorted(rules, reverse=True) :
    #       print "(%d -> %d) :\t%f" % (t1, t2, interest) - freqs1[t2]/total
    #       print "(%d -> %d) :\t%f" % (t2, t1, interest) - freqs1[t1]/total

    return rules


  def get_topics_layer_from_db(self, doc_ids, min_conf_topics):
    """
    Run topic modeling for the content on the given papers and assemble the topic nodes
    and edges.
    """
    #       topics, doc_topics, tokens = topic_modeling.get_topics_online(doc_ids, ntopics=200, beta=0.1,
    #                                                                                                                               cache_folder=self.cache_folder, ign_cache=False)

    # Build topic nodes and paper-topic edges
    topic_nodes = set()
    topic_paper_edges = set()

    # Retrieve top topics for each document from the db
    topic_ids_per_doc = []
    for doc_id in doc_ids:

      topics = db.select(fields=["topic_id", "value"], table="doc_topics", where="paper_id='%s'" % doc_id)
      if len(topics):
        topic_ids, topic_values = zip(*topics)

        topic_ids_per_doc.append(topic_ids)
        #               topic_values_per_doc.append(topic_values)

        topic_nodes.update(topic_ids)
        topic_paper_edges.update([(doc_id, topic_ids[t], topic_values[t]) for t in xrange(len(topic_ids))])

      #         for d in xrange(len(doc_ids)) :
      #             topic_ids = topic_ids_per_doc[d]
      #             topic_values = topic_values_per_doc[d]


    # Normalize edge weights with the maximum value
    topic_paper_edges = normalize_edges(topic_paper_edges)

    # From the list of relevant topics f
    #       rules = self.get_frequent_topic_pairs(topic_ids_per_doc, min_conf_topics)
    topic_topic_edges = get_rules_by_lift(topic_ids_per_doc, min_conf_topics)
    topic_topic_edges = normalize_edges(topic_topic_edges)

    # Get the density of the ngram layer to feel the effect of 'min_topics_lift'
    self.topic_density = float(len(topic_topic_edges)) / len(topic_nodes)

    #       get_name = lambda u: db.select_one(fields="words", table="topic_words", where="topic_id=%d"%u)
    #       top = sorted(topic_topic_edges, key=lambda t:t[2], reverse=True)
    #       for u, v, w in top :
    #           uname = get_name(u)
    #           vname = get_name(v)
    #           print "%s\n%s\n%.3f\n" % (uname, vname, w)

    # Cast topic_nodes to list so we can assure element order
    topic_nodes = list(topic_nodes)

    return topic_nodes, topic_topic_edges, topic_paper_edges


  # def get_topics_layer(self, doc_ids, min_conf_topics) :
  #     '''
  #     Run topic modeling for the content on the given papers and assemble the topic nodes
  #     and edges.
  #     '''
  #     topics, doc_topics, tokens = topic_modeling.get_topics_online(self.cache_folder, ntopics=200,
  #                                                                                                                             beta=0.1, ign_cache=False)
  #
  #     doc_topic_above = DOC_TOPIC_THRES
  #
  #     topic_nodes = set()
  #     topic_paper_edges = set()
  #     topics_per_document = []
  #     for d in xrange(len(doc_ids)) :
  #         relevant_topics = self.get_relevant_topics(doc_topics[d], above=doc_topic_above)
  #
  #         # This data structure is needed for the correlation between topics
  #         topics_per_document.append(relevant_topics)
  #
  #         topic_nodes.update(relevant_topics)
  #         topic_paper_edges.update([(doc_ids[d], t, doc_topics[d][t]) for t in relevant_topics])
  #
  #     # Normalize edge weights with the maximum value
  #     topic_paper_edges = normalize_edges(topic_paper_edges)
  #
  #     # From the list of relevant topics f
  #     rules = self.get_frequent_topic_pairs(topics_per_document)
  #
  #     # Add only edges above certain confidence. These edge don't
  #     # need to be normalized since 0 < confidence < 1.
  #     topic_topic_edges = set()
  #     for interest, (t1, t2) in rules :
  #         if interest >= min_conf_topics :
  #             topic_topic_edges.add( (t1, t2, interest) )
  #
  #     # Cast topic_nodes to list so we can assure element order
  #     topic_nodes = list(topic_nodes)
  #
  #     # Select only the names of the topics being considered here
  #     # and store in a class attribute
  #     topic_names = topic_modeling.get_topic_names(topics, tokens)
  #     self.topic_names = {tid: topic_names[tid] for tid in topic_nodes}
  #
  #     return topic_nodes, topic_topic_edges, topic_paper_edges, tokens


  # def get_words_layer_from_db(self, doc_ids):
  #     '''
  #     Create words layers by retrieving TF-IDF values from the DB (previously calculated).
  #     '''
  #
  #     word_nodes = set()
  #     paper_word_edges = set()
  #
  #     for doc_id in doc_ids :
  #         rows = db.select(fields=["word", "value"],
  #                                          table="doc_words",
  #                                          where="paper_id='%s'"%doc_id,
  #                                          order_by=("value","desc"),
  #                                          limit=5)
  #         top_words, top_values = zip(*rows)
  #
  #         word_nodes.update(top_words)
  #         paper_word_edges.update([(doc_id, top_words[t], top_values[t]) for t in range(len(top_words))])
  #
  #     # Normalize edges weights by their biggest value
  #     paper_word_edges = normalize_edges(paper_word_edges)
  #
  #     return word_nodes, paper_word_edges


  # def get_ngrams_layer_from_db2(self, doc_ids):
  #     '''
  #     Create words layers by retrieving TF-IDF values from the DB (previously calculated).
  #     '''
  #     word_nodes = set()
  #     paper_word_edges = set()
  #
  #     ngrams_per_doc = []
  #     for doc_id in doc_ids :
  #         rows = db.select(fields=["ngram", "value"],
  #                                          table="doc_ngrams",
  #                                          where="(paper_id='%s') AND (value>=%f)" % (doc_id, config.MIN_NGRAM_TFIDF))
  #
  #
  #         if (len(rows) > 0) :
  #             top_words, top_values = zip(*rows)
  #
  #             word_nodes.update(top_words)
  #             paper_word_edges.update([(doc_id, top_words[t], top_values[t]) for t in range(len(top_words))])
  #
  #             ngrams_per_doc.append(top_words)
  #
  #     ## TEMPORARY ##
  #     # PRINT MEAN NGRAMS PER DOC
  ##        mean_ngrams = np.mean([len(ngrams) for ngrams in ngrams_per_doc])
  ##        print "%f\t" % mean_ngrams,
  #
  #     # Get get_rules_by_lift between co-occurring ngrams to create edges between ngrams
  #     word_word_edges = get_rules_by_lift(ngrams_per_doc, min_lift=config.MIN_NGRAM_LIFT)
  #
  ##        print len(word_nodes), "word nodes."
  ##        print len(word_word_edges), "word-word edges."
  ##        for e in word_word_edges :
  ##            print e
  #
  ##        for rule in sorted(rules, reverse=True) :
  ##            print rule
  #
  #     # Normalize edges weights by their biggest value
  #     word_word_edges = normalize_edges(word_word_edges)
  #     paper_word_edges = normalize_edges(paper_word_edges)
  #
  #     return word_nodes, word_word_edges, paper_word_edges


  def get_ngrams_layer_from_db(self, doc_ids, min_ngram_lift):
    """
    Create words layers by retrieving TF-IDF values from the DB (previously calculated).
    """
    word_nodes = set()
    paper_word_edges = list()

    doc_ids_str = ",".join(["'%s'" % doc_id for doc_id in doc_ids])

    MIN_NGRAM_TFIDF = 0.25

    table = "doc_ngrams"
    rows = db.select(fields=["paper_id", "ngram", "value"], table=table,
             where="paper_id IN (%s) AND (value>=%f)" % (doc_ids_str, MIN_NGRAM_TFIDF))

    #
    ngrams_per_doc = defaultdict(list)
    for doc_id, ngram, value in rows:
      word_nodes.add(ngram)
      paper_word_edges.append((str(doc_id), ngram, value))

      ngrams_per_doc[str(doc_id)].append(ngram)

    # Get get_rules_by_lift between co-occurring ngrams to create edges between ngrams
    word_word_edges = get_rules_by_lift(ngrams_per_doc.values(), min_lift=min_ngram_lift)

    # Get the density of the ngram layer to feel the effect of 'min_ngram_lift'
    self.ngram_density = float(len(word_word_edges)) / len(word_nodes)
    self.nwords = len(word_nodes)

    # Normalize edges weights by their biggest value
    word_word_edges = normalize_edges(word_word_edges)
    paper_word_edges = normalize_edges(paper_word_edges)

    return word_nodes, word_word_edges, paper_word_edges


  def get_keywords_layer_from_db(self, doc_ids, min_ngram_lift):
    """
    Create words layers by retrieving TF-IDF values from the DB (previously calculated).
    """
    word_nodes = set()
    paper_word_edges = list()

    doc_ids_str = ",".join(["'%s'" % doc_id for doc_id in doc_ids])

    where = "paper_id IN (%s)" % doc_ids_str
    if config.KEYWORDS == "extracted":
      where += " AND (extracted=1)"

    elif config.KEYWORDS == "extended":
      where += " AND (extracted=0) AND (value>=%f)" % config.MIN_NGRAM_TFIDF

    elif config.KEYWORDS == "both":
      where += " AND (value>=%f)" % config.MIN_NGRAM_TFIDF

    rows = db.select(fields=["paper_id", "keyword_name"],
             table="paper_keywords",
             where=where)

    #
    ngrams_per_doc = defaultdict(list)
    for doc_id, ngram in rows:
      word_nodes.add(ngram)
      paper_word_edges.append((str(doc_id), ngram, 1.0))

      ngrams_per_doc[str(doc_id)].append(ngram)

    # Get get_rules_by_lift between co-occurring ngrams to create edges between ngrams
    word_word_edges = get_rules_by_lift(ngrams_per_doc.values(), min_lift=min_ngram_lift)

    # Get the density of the ngram layer to feel the effect of 'min_ngram_lift'
    self.ngram_density = float(len(word_word_edges)) / len(word_nodes)
    self.nwords = len(word_nodes)

    # Normalize edges weights by their biggest value
    word_word_edges = normalize_edges(word_word_edges)
    paper_word_edges = normalize_edges(paper_word_edges)

    return word_nodes, word_word_edges, paper_word_edges


  def get_papers_atts(self, papers):
    """
    Fetch attributes for each paper from the DB.
    """
    atts = {}
    for paper in papers:
      title, jornal, conf = db.select_one(["normal_title", "jornal_id", "conf_id"], table="papers", where="id='%s'" % paper)
      title = title if title else ""
      venue = jornal if jornal else conf
      atts[paper] = {"label": title, "title": title, "venue": venue}

    return atts


  def get_authors_atts(self, authors):
    """
    Fetch attributes for each author from the DB.
    """
    atts = {}
    for author in authors:
      name, email, affil = db.select_one(["name", "email", "affil"], table="authors", where="cluster=%d" % author)
      npapers = str(db.select_one("count(*)", table="authors", where="cluster=%d" % author))
      name = name if name else ""
      email = email if email else ""
      affil = affil if affil else ""

      atts[author] = {"label": name, "name": name, "email": email, "affil": affil, "npapers": npapers}

    return atts


  def get_topics_atts(self, topics):
    """
    Fetch attributes for each topic.
    """
    topic_names = db.select(fields="words", table="topic_words", order_by="topic_id")
    atts = {}
    for topic in topics:
      topic_name = topic_names[topic]
      atts[topic] = {"label": topic_name, "description": topic_name}

    return atts


  def get_words_atts(self, words):
    """
    Fetch attributes for each word.
    """
    atts = {}
    for word in words:
      atts[word] = {"label": word}

    return atts


  def assemble_layers(self, pubs, citation_edges,
            authors, auth_auth_edges, coauth_edges, auth_edges,
            # topics, topic_topic_edges, paper_topic_edges,
            # ngrams, ngram_ngram_edges, paper_ngram_edges,
            # venues, pub_venue_edges,
            affils, author_affil_edges, affil_affil_edges, affil_scores=None, author_scores=None):
    """
    Assembles the layers as an unified graph. Each node as an unique id, its type (paper,
    author, etc.) and a readable label (paper title, author name, etc.)
    """
    graph = nx.DiGraph()

    # These map the original identifiers for each type (paper doi, author id,
    # etc.) to the new unique nodes id.
    pubs_ids = {}
    authors_ids = {}
    # topics_ids = {}
    # words_ids = {}
    # venues_ids = {}
    affils_ids = {}

    # Controls the unique incremental id generation
    next_id = 0

    # Add each paper providing an unique node id. Some attributes must be added
    # even if include_attributes is True, since they are used in ranking algorithm.
    if pubs:
      for pub in pubs:
        pub = str(pub)

        #         if hasattr(self, 'query_sims') :
        #             query_score = float(self.query_sims[paper])  #if paper in self.query_sims else 0.0
        #         else :
        #             query_score = 0.0

        graph.add_node(next_id,
                 type="paper",
                 entity_id=pub,
                 year=self.pub_years[pub] if self.pub_years.has_key(pub) else 0
                 )

        pubs_ids[pub] = next_id
        next_id += 1

      # Add citation edges (directed)
      for paper1, paper2, weight in citation_edges:
        graph.add_edge(pubs_ids[paper1], pubs_ids[paper2], weight=weight)
        # graph.add_edge(pubs_ids[paper2], pubs_ids[paper1], weight=weight) # try undirected, bad


    # Add each author providing an unique node id
    if authors:
      if not author_scores:
        for author in authors:
          graph.add_node(next_id, type="author", entity_id=author)

          authors_ids[author] = next_id
          next_id += 1

      else:
        for author in authors:
          graph.add_node(next_id, type="author", entity_id=author, author_score=author_scores[author])

          authors_ids[author] = next_id
          next_id += 1



      # Add author-author (citation) edges on both directions (undirected)
      if auth_auth_edges:
        for author1, author2, weight in auth_auth_edges:
          graph.add_edge(authors_ids[author1], authors_ids[author2], weight=weight)


      # Add coauthor edges on both directions (undirected)
      if coauth_edges:
        for author1, author2, weight in coauth_edges:
          graph.add_edge(authors_ids[author1], authors_ids[author2], weight=weight)
          graph.add_edge(authors_ids[author2], authors_ids[author1], weight=weight)

      # Add authorship edges on both directions (undirected)
      if auth_edges:
        for paper, author in auth_edges:
          graph.add_edge(pubs_ids[paper], authors_ids[author], weight=1.0)
          graph.add_edge(authors_ids[author], pubs_ids[paper], weight=1.0)


    ####################################

    #       # Add topic nodes
    #       for topic in topics :
    #           graph.add_node(next_id, type="topic", entity_id=topic)
    #
    #           topics_ids[topic] = next_id
    #           next_id += 1
    #
    #       # Add topic correlation edges (directed)
    #       for topic1, topic2, weight in topic_topic_edges :
    #           graph.add_edge(topics_ids[topic1], topics_ids[topic2], weight=weight)
    #           graph.add_edge(topics_ids[topic2], topics_ids[topic1], weight=weight)
    #
    #       # Add paper-topic edges (directed)
    #       for paper, topic, weight in paper_topic_edges :
    #           graph.add_edge(pubs_ids[paper], topics_ids[topic], weight=weight)
    #           graph.add_edge(topics_ids[topic], pubs_ids[paper], weight=weight)

    ####################################
    # # Add ngram nodes
    # for ngram in ngrams:
    #   graph.add_node(next_id, type="keyword", entity_id=ngram)

    #   words_ids[ngram] = next_id
    #   next_id += 1

    # #        Add word-word edges (undirected)
    # for w1, w2, weight in ngram_ngram_edges:
    #   graph.add_edge(words_ids[w1], words_ids[w2], weight=weight)
    #   graph.add_edge(words_ids[w2], words_ids[w1], weight=weight)

    # # Add paper-word edges (undirected)
    # for paper, word, weight in paper_ngram_edges:
    #   graph.add_edge(pubs_ids[paper], words_ids[word], weight=weight)
    #   graph.add_edge(words_ids[word], pubs_ids[paper], weight=weight)

    ####################################
    # Add venues to the graph
    # for venue in venues:
    #   graph.add_node(next_id, type="venue", entity_id=venue)

    #   venues_ids[venue] = next_id
    #   next_id += 1

    # for pub, venue, weight in pub_venue_edges:
    #   graph.add_edge(pubs_ids[pub], venues_ids[venue], weight=weight)
    #   graph.add_edge(venues_ids[venue], pubs_ids[pub], weight=weight)


    # Add affils to the graph
    if affils:
      if not affil_scores:
        for affil in affils:
          graph.add_node(next_id, type="affil", entity_id=affil)

          affils_ids[affil] = next_id
          next_id += 1

      else:
        for affil in affils:
          graph.add_node(next_id, type="affil", entity_id=affil, affil_score=affil_scores[affil])

          affils_ids[affil] = next_id
          next_id += 1



      if author_affil_edges:
        # author_affils_dict = defaultdict()
        for author, affil, weight in author_affil_edges:
          graph.add_edge(authors_ids[author], affils_ids[affil], weight=weight)
          graph.add_edge(affils_ids[affil], authors_ids[author], weight=weight)
          # try:
          #   author_affils_dict[author].add(affil)
          # except:
          #   author_affils_dict[author] = set([affil])


      # try affil-affil layer
      if affil_affil_edges:
        # 1)
        for affil1, affil2, weight in affil_affil_edges:
          graph.add_edge(affils_ids[affil1], affils_ids[affil2], weight=weight)

        # 2)
        # for author1, author2, weight in coauth_edges:
        #   if author_affils_dict.has_key(author1) and author_affils_dict.has_key(author2):
        #     for affil1 in author_affils_dict[author1]:
        #       for affil2 in author_affils_dict[author2]:
        #         if affil1 != affil2:
        #           graph.add_edge(affils_ids[affil1], affils_ids[affil2], weight=weight)
        #           graph.add_edge(affils_ids[affil2], affils_ids[affil1], weight=weight)

    # Get the attributes for each author
    # Get attributes for each paper
    if self.include_attributes:
      # add_attributes(graph, pubs, pubs_ids, self.get_papers_atts(pubs))
      # add_attributes(graph, authors, authors_ids, self.get_authors_atts(authors))
      # add_attributes(graph, topics, topics_ids, self.get_topics_atts(topics))
      # add_attributes(graph, words, words_ids, self.get_words_atts(words))
      pass
    return graph


  def parse_tfidf_line(self, line):
    parts = line.strip().split()
    tokens = parts[0::2]
    tfidf = map(float, parts[1::2])
    return dict(zip(tokens, tfidf))


  def get_edge_contexts(self, papers, citation_edges):

    citation_edges = set(citation_edges)

    tokens_per_citation = {}
    for citing in papers:
      if os.path.exists(config.CTX_PATH % citing):
        with open(config.CTX_PATH % citing, "r") as file:
          for line in file:
            cited, tokens_tfidf = line.strip().split('\t')

            if (citing, cited) in citation_edges:
              tokens_per_citation[(citing, cited)] = self.parse_tfidf_line(tokens_tfidf)

    return tokens_per_citation


  def get_venues_layer(self, papers):
    """
    Returns the venues' ids and edges from publications to venues according
    to the venues used in the publications.
    """
    if hasattr(papers, '__iter__'):
      if len(papers) == 0:
        return [], []
      else:
        paper_str = ",".join(["'%s'" % paper_id for paper_id in papers])
    else:
      raise TypeError("Parameter 'papers' is of unsupported type. Iterable needed.")

    venues = set()
    pub_venue_edges = list()
    rows = db.select(fields=["id", "jornal_id", "conf_id"], table="papers", \
            where="id IN (%s)"%paper_str)
    for pub, jornal_id, conf_id in rows:
      if jornal_id:
        venues.add(jornal_id)
        pub_venue_edges.append((pub, jornal_id, 1.0))
      elif conf_id:
        venues.add(conf_id)
        pub_venue_edges.append((pub, conf_id, 1.0))

    return list(venues), pub_venue_edges



  def get_affils_layer(self, authors, related_papers):
    """
    Returns the affils' ids and edges from authors to affiliations according
    to authors.
    """
    if hasattr(authors, '__iter__'):
      if len(authors) == 0:
        return [], []
      else:
        author_str = ",".join(["'%s'" % author_id for author_id in authors])
    else:
      raise TypeError("Parameter 'authors' is of unsupported type. Iterable needed.")

    if hasattr(related_papers, '__iter__'):
      if len(related_papers) == 0:
        return [], []
      else:
        paper_str = ",".join(["'%s'" % paper_id for paper_id in related_papers])
    else:
      raise TypeError("Parameter 'related_papers' is of unsupported type. Iterable needed.")


    affils = set()
    author_affil_edges = set()
    affil_affil_edges = set()
    rows = db.select(["paper_id", "author_id", "affil_id"], "paper_author_affils",\
           where="author_id IN (%s) and paper_id IN (%s)"%(author_str, paper_str))

    count = 0
    get_affil_count = 0
    author = set()
    retrieved_paper_authors = set()
    for paper_id, author_id, affil_id in rows:
        if affil_id == '' and not author_id in author:
            count += 1
            author.add(author_id)
        if not affil_id:
            if not (paper_id, author_id) in retrieved_paper_authors:
                # print "author id: %s"%author_id
                # print "paper id: %s"%paper_id
                # import pdb;pdb.set_trace()
                # retrieved_affil_ids = None # turn off
                retrieved_affil_ids, flag = retrieve_affils_by_authors(author_id, table_name='dblp', paper_id=paper_id)
                if flag == 1:
                    get_affil_count += 1
                # retrieved_affil_ids = retrieve_affils_by_author_papers(author_id, paper_id, table_name='csx')
                if retrieved_affil_ids:
                    retrieved_paper_authors.add((paper_id, author_id))
                    # print "retrieved", (paper_id, author_id, retrieved_affil_ids)
                    #############
                    # update db
                    #############
                    # to do

                    affil_id = set(retrieved_affil_ids)
                else:
                    # skip this record
                    continue
            else:
                continue

            # continue

        else:
            affil_id = set([affil_id])

        author.add(author_id)
        for each_affil in affil_id:
            affils.add(each_affil)
            author_affil_edges.add((author_id, each_affil, 1.0))

    print "missing author: %s/%s"%(count, len(author))
    print "get_affil_count: %s"%get_affil_count

    # missing_count = 0
    # missing_author = 0
    # hit_count = 0
    # author_affils = defaultdict()
    # for paper_id, author_id, affil_id in rows:
    #   if affil_id == '':
    #     missing_count += 1
    #   try:
    #     author_affils[author_id].add(affil_id)
    #   except:
    #     author_affils[author_id] = set([affil_id])

    # for author_id, affil_ids in author_affils.iteritems():
    #   # add affil-affil edges
    #   # for i in range(len(affil_ids)-1):
    #   #   if list(affil_ids)[i] != '':
    #   #     for j in range(i+1,len(affil_ids)):
    #   #       if list(affil_ids)[j] != '':
    #   #         affil_affil_edges.add((list(affil_ids)[i], list(affil_ids)[j], 1.0))


    #   for each in affil_ids:
    #     if each != '':
    #       affils.add(each)
    #       author_affil_edges.add((author_id, each, 1.0))
    #     else:
    #       # To be improved, we only retrieve affils
    #       # when we don't know any affils which the author belongs to
    #       if len(affil_ids) == 1:
    #         # import pdb;pdb.set_trace()
    #         missing_author += 1
    #         # we check external data (e.g., csx dataset) and do string matching which is knotty.
    #         match_affil_ids, _ = retrieve_affils_by_authors(author_id, table_name='dblp')
    #         if match_affil_ids:
    #           hit_count += 1
    #         for each_affil in match_affil_ids:
    #           affils.add(each_affil)
    #           author_affil_edges.add((author_id, each_affil, 1.0))

    # print "missing count: %s"%missing_count
    # print "missing author: %s"%missing_author
    # print "hit count: %s"%hit_count

    # # count = 0
    # tmp_authors = set()
    # for paper_id, author_id, affil_id in rows:
    #   # Since coverage of affils in MAG dataset is quite low,
    #   # we use data from CSX dataset as a complement.
    #   if affil_id == '':
    #     try:
    #       if author_id in tmp_authors: # For the sake of simplicity, we don't look up affils in this case.
    #         continue

    #       # count += 1
    #       tmp_authors.add(author_id)
    #       # import pdb;pdb.set_trace()

    #       # 1) first, we check paper_author_affils table.
    #       if author_id in rows
    #       # 2) if 1) fails, then we check csx_authors table and do string matching which is knotty.
    #       match_affil_ids = retrieve_affils_by_authors(author_id)

    #       if match_affil_ids:
    #         affils.update(match_affil_ids)
    #         for each in match_affil_ids:
    #           author_affil_edges.add((author_id, each, 1.0))

    #     except Exception, e:
    #       print e
    #       continue

    #   else:
    #     affils.add(affil_id)
    #     author_affil_edges.add((author_id, affil_id, 1.0))


    # print len(affils), len(author_affil_edges), len(affil_affil_edges)
    return list(affils), list(author_affil_edges), list(affil_affil_edges)


  def get_paper_affils(self, conf_name, year, age_relev, exclude=[], expanded_year=[]):

    current_year = config.PARAMS['current_year']
    old_year = config.PARAMS['old_year']

    pubs, _, affils = get_selected_expand_pubs(conf_name, year)
    docs = set(pubs.keys()) - set(exclude)

    if expanded_year:
      conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf_name, limit=1)[0]
      pubs2, _, affils2 = get_selected_expand_pubs(conf_id, expanded_year, _type="expanded")
      docs2 = set(pubs2.keys()) - set(exclude)
      docs.update(docs2)
      pubs.update(pubs2)
      affils.update(affils2)

    self.edges_lookup = GraphBuilder(get_all_edges(docs))
    edges = self.edges_lookup.subgraph(docs)
    weighted_edges = self.get_weights_file(edges)

    paper_authors = defaultdict()
    paper_affils = defaultdict()
    for each in docs:
      author1 = set(pubs[each]['author'].keys())
      affil1 = set([y for x in pubs[each]['author'].values() for y in x])
      paper_authors[each] = author1
      paper_affils[each] = affil1

    return docs, weighted_edges, paper_authors, paper_affils


  def get_projected_affils_layer(self, conf_name, year, age_relev, exclude=[], expanded_year=[]):
    """
    directly projects paper and author layers onto affil layer.
    """

    current_year = config.PARAMS['current_year']
    old_year = config.PARAMS['old_year']

    pubs, _, affils = get_selected_expand_pubs(conf_name, year)
    docs = set(pubs.keys()) - set(exclude)

    if expanded_year:
      conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf_name, limit=1)[0]
      pubs2, _, affils2 = get_selected_expand_pubs(conf_id, expanded_year, _type="expanded")
      docs2 = set(pubs2.keys()) - set(exclude)
      docs.update(docs2)
      pubs.update(pubs2)
      affils.update(affils2)


    self.edges_lookup = GraphBuilder(get_all_edges(docs))
    edges = self.edges_lookup.subgraph(docs)

    affil_affils = defaultdict()
    for k, v in edges:
      # paper k cites paper v
      year = min(max(pubs[k]['year'], old_year), current_year)
      age_decay = np.exp(-(age_relev)*(current_year-year)) # ranges from [0, 1]

      affils1 = set([y for x in pubs[k]['author'].values() for y in x])
      affils2 = set([y for x in pubs[v]['author'].values() for y in x])

      for affil1 in affils1:
        for affil2 in affils2:
          if affil1 != affil2:
            if affil_affils.has_key(affil1):
              if affil_affils[affil1].has_key(affil2):
                affil_affils[affil1][affil2] += age_decay
              else:
                affil_affils[affil1][affil2] = age_decay
            else:
              affil_affils[affil1] = {affil2: age_decay}
    # import pdb;pdb.set_trace()

    affil_affil_edges = set()
    for k, v in affil_affils.iteritems():
      for each, count in v.iteritems():
        weight = np.log10(1.0 + count)
        affil_affil_edges.add((k, each, weight))




    # if expand_method == 'n_hops':

    #   # Get doc ids as uni-dimensional list
    #   self.edges_lookup = GraphBuilder(get_all_edges(docs))
    #   nodes = set(docs)

    #   # Expand the docs set by reference
    #   nodes = self.get_expanded_pubs_by_nhops(nodes, self.edges_lookup, exclude_list, n_hops)


    # elif expand_method == 'conf':

    #   # Expand the docs by getting more papers from the targeted conference
    #   # expanded_pubs = self.get_expanded_pubs_by_conf(conf_name, [2009, 2010])
    #   nodes = set(docs)
    #   expanded_year = []
    #   # expanded_year = range(2005, 2011)
    #   expanded_pubs = self.get_expanded_pubs_by_conf2(conf_name, expanded_year)

    #   # add year
    #   for paper, year in expanded_pubs:
    #     self.pub_years[paper] = year

    #   # Remove documents from the exclude list and keep only processed ids
    #   if expanded_pubs:
    #     expanded_docs = set(zip(*expanded_pubs)[0]) - set(exclude_list)
    #     nodes.update(expanded_docs)

    #   self.edges_lookup = GraphBuilder(get_all_edges(nodes))

    # else:
    #   raise ValueError("parameter expand_method should either be n_hops or conf.")
    return affils, affil_affil_edges




  def get_projected_author_layer(self, conf_name, year, age_relev, exclude=[], expanded_year=[], expand_conf_year=[]):
    """
    projects paper layer onto author layer.
    """
    print 'age_relev: %s' % age_relev

    current_year = config.PARAMS['current_year']
    old_year = config.PARAMS['old_year']

    pubs, authors, _ = get_selected_expand_pubs(conf_name, year)
    docs = set(pubs.keys()) - set(exclude)

    if expanded_year:
      conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf_name, limit=1)[0]
      pubs2, authors2, _ = get_selected_expand_pubs(conf_id, expanded_year, _type="expanded")
      docs2 = set(pubs2.keys()) - set(exclude)
      docs.update(docs2)
      pubs.update(pubs2)
      authors.update(authors2)


    # expand docs set by getting more papers accepted by related conferences
    for conf, years in expand_conf_year:
      conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf, limit=1)[0]
      pubs2, authors2, _ = get_selected_expand_pubs(conf_id, years, _type='expanded')
      docs2 = set(pubs2.keys()) - set(exclude)
      docs.update(docs2)
      pubs.update(pubs2)
      authors.update(authors2)


    self.edges_lookup = GraphBuilder(get_all_edges(docs))
    edges = self.edges_lookup.subgraph(docs)

    # import pdb;pdb.set_trace()


    # coauthor edges
    author_affils = defaultdict(set)
    co_authors = defaultdict()
    for each_doc in docs:
      # add author-affils
      for k, v in pubs[each_doc]['author'].iteritems():
        author_affils[k].update(v)


      year = min(max(pubs[each_doc]['year'], old_year), current_year)
      age_decay = np.exp(-(age_relev)*(current_year-year)) # ranges from [0, 1]

      auths = list(set(pubs[each_doc]['author'].keys()))
      for i in xrange(len(auths) - 1):
        for j in xrange(i + 1, len(auths)):
          auth1 = min(auths[i], auths[j])
          auth2 = max(auths[i], auths[j])
          if co_authors.has_key(auth1):
            if co_authors[auth1].has_key(auth2):
              co_authors[auth1][auth2] += age_decay
            else:
              co_authors[auth1][auth2] = age_decay
          else:
            co_authors[auth1] = {auth2: age_decay}

    coauthor_edges = set()
    for k, v in co_authors.iteritems():
      for each, count in v.iteritems():
        weight = np.log10(1.0 + count)
        coauthor_edges.add((k, each, weight))


    # author-author edges
    author_authors = defaultdict()
    for k, v in edges:
      # paper k cites paper v
      year = min(max(pubs[k]['year'], old_year), current_year)
      age_decay = np.exp(-(age_relev)*(current_year-year)) # ranges from [0, 1]

      authors1 = set([x for x in pubs[k]['author'].keys()])
      authors2 = set([x for x in pubs[v]['author'].keys()])

      for author1 in authors1:
        for author2 in authors2:
          if author1 != author2:
            if author_authors.has_key(author1):
              if author_authors[author1].has_key(author2):
                author_authors[author1][author2] += age_decay
              else:
                author_authors[author1][author2] = age_decay
            else:
              author_authors[author1] = {author2: age_decay}

    author_author_edges = set()
    for k, v in author_authors.iteritems():
      for each, count in v.iteritems():
        weight = np.log10(1.0 + count)
        author_author_edges.add((k, each, weight))

    return authors, author_author_edges, coauthor_edges, author_affils




  def get_paper_author_dist(self, conf_name, year, age_relev, exclude=[], expanded_year=[]):
    """
    get paper and author distribution.
    """

    current_year = config.PARAMS['current_year']
    old_year = config.PARAMS['old_year']

    pubs, authors, _ = get_selected_expand_pubs(conf_name, year)
    docs = set(pubs.keys()) - set(exclude)

    if expanded_year:
      conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf_name, limit=1)[0]
      pubs2, authors2, _ = get_selected_expand_pubs(conf_id, expanded_year, _type="expanded")
      docs2 = set(pubs2.keys()) - set(exclude)
      docs.update(docs2)
      pubs.update(pubs2)
      authors.update(authors2)



    self.edges_lookup = GraphBuilder(get_all_edges(docs))
    edges = self.edges_lookup.subgraph(docs)

    # import pdb;pdb.set_trace()


    # coauthor edges
    author_affils = defaultdict(set)
    author_npubs = dict.fromkeys(authors, 0.0)
    co_authors = defaultdict()
    for each_doc in docs:
      # add author-affils
      for k, v in pubs[each_doc]['author'].iteritems():
        author_npubs[k] += 1
        author_affils[k].update(v)

      year = min(max(pubs[each_doc]['year'], old_year), current_year)
      age_decay = np.exp(-(age_relev)*(current_year-year)) # ranges from [0, 1]

      auths = list(set(pubs[each_doc]['author'].keys()))
      for i in xrange(len(auths) - 1):
        for j in xrange(i + 1, len(auths)):
          auth1 = min(auths[i], auths[j])
          auth2 = max(auths[i], auths[j])
          if co_authors.has_key(auth1):
            if co_authors[auth1].has_key(auth2):
              co_authors[auth1][auth2] += age_decay
            else:
              co_authors[auth1][auth2] = age_decay
          else:
            co_authors[auth1] = {auth2: age_decay}


    coauthor_edges = set()
    for k, v in co_authors.iteritems():
      for each, count in v.iteritems():
        weight = np.log10(1.0 + count)
        coauthor_edges.add((k, each, weight))


    # # of authors per paper distribution
    paper_nauthor = {x:len(pubs[x]['author']) for x in docs}
    author_per_paper_dist = defaultdict(float)
    for _, v in paper_nauthor.iteritems():
      author_per_paper_dist[v] += 1.0



    # # of citations per author
    author_ncites = dict.fromkeys(authors, 0.0)

    for k, v in edges:
      # paper k cites paper v
      year = min(max(pubs[k]['year'], old_year), current_year)
      age_decay = np.exp(-(age_relev)*(current_year-year)) # ranges from [0, 1]

      auths = set([x for x in pubs[v]['author'].keys()])
      for each_author in auths:
        try:
          author_ncites[each_author] += age_decay
        except:
          author_ncites[each_author] = age_decay



    return authors, coauthor_edges, author_affils, author_per_paper_dist, author_ncites, author_npubs




  def build(self, conf_name, year, n_hops, min_topic_lift, min_ngram_lift, exclude=[], expanded_year=[], expand_conf_year=[]):
    """
    Build graph model from given conference.
    """

    log.debug("Building model for conference='%s' and hops=%d." % (conf_name, n_hops))

    pubs, citation_edges = self.get_pubs_layer(conf_name, year, n_hops, set(exclude), expanded_year, expand_conf_year)
    log.debug("%d pubs and %d citation edges." % (len(pubs), len(citation_edges)))
    print "%d pubs and %d citation edges." % (len(pubs), len(citation_edges))
    authors, coauth_edges, auth_edges = self.get_authors_layer(pubs)
    log.debug("%d authors, %d co-authorship edges and %d authorship edges." % (
      len(authors), len(coauth_edges), len(auth_edges)))

    #       topics, topic_topic_edges, pub_topic_edges = self.get_topics_layer_from_db(pubs, min_topic_lift)
    #       log.debug("%d topics, %d topic-topic edges and %d pub-topic edges."
    #                                       % (len(topics), len(topic_topic_edges), len(pub_topic_edges)))

    # # Use the standard ngrams formulation if the config says so
    # if config.KEYWORDS == "ngrams":
    #   words, word_word_edges, pub_word_edges = self.get_ngrams_layer_from_db(pubs, min_ngram_lift)

    # # Otherwise use some variant of a keywords' layer
    # else:
    #   words, word_word_edges, pub_word_edges = self.get_keywords_layer_from_db(pubs, min_ngram_lift)
    # log.debug("%d words and %d pub-word edges." % (len(words), len(pub_word_edges)))

    # venues, pub_venue_edges = self.get_venues_layer(pubs)
    # log.debug("%d venues and %d pub-venue edges." % (len(venues), len(pub_venue_edges)))

    affils, author_affil_edges, affil_affil_edges = self.get_affils_layer(authors, pubs)
    log.debug("%d affiliations and %d pub-affil edges." % (len(affils), len(author_affil_edges)))

    graph = self.assemble_layers(pubs, citation_edges,
                   authors, None, coauth_edges, auth_edges,
                   # None, None, None,
                   #                                                        topics, topic_topic_edges, pub_topic_edges,
                   # words, word_word_edges, pub_word_edges,
                   # venues, pub_venue_edges,
                   affils, author_affil_edges, None)

    # Writes the contexts of each edge into a file to be used efficiently
    # on the ranking algorithm.
    #       self.write_edge_contexts(papers, citation_edges, ctxs_file)

    # Writes the gexf
    #       write_graph(graph, model_file)
    return graph


  def build_affils(self, conf_name, year, age_relev, n_hops, exclude=[], expanded_year=[]):
    """
    Build graph model from given conference.
    """

    # log.debug("Building model for conference='%s' and hops=%d." % (conf_name, n_hops))

    # pubs, citation_edges = self.get_pubs_layer(conf_name, year, n_hops, set(exclude))
    # log.debug("%d pubs and %d citation edges." % (len(pubs), len(citation_edges)))
    # print "%d pubs and %d citation edges." % (len(pubs), len(citation_edges))
    # authors, coauth_edges, auth_edges = self.get_authors_layer(pubs)
    # log.debug("%d authors, %d co-authorship edges and %d authorship edges." % (
    #   len(authors), len(coauth_edges), len(auth_edges)))

    affils, affil_affil_edges = self.get_projected_affils_layer(conf_name, year, age_relev, exclude, expanded_year)

    graph = self.assemble_layers(None, None,
                   None, None, None, None,
                   affils, None, affil_affil_edges)

    # Writes the contexts of each edge into a file to be used efficiently
    # on the ranking algorithm.
    #       self.write_edge_contexts(papers, citation_edges, ctxs_file)

    # Writes the gexf
    #       write_graph(graph, model_file)
    return graph


  def get_ranked_affils_by_papers(self, conf_name, year, age_relev, n_hops, alpha, exclude=[], expanded_year=[]):

    docs, citation_edges, _, paper_affils = self.get_paper_affils(conf_name, year, age_relev, exclude, expanded_year)
    # 1) run pagerank on paper layer
    graph = self.assemble_layers(docs, citation_edges,
                   None, None, None, None,
                   None, None, None)

    paper_scores = rank_single_layer_nodes(graph, alpha=alpha)
    paper_scores = {graph.node[nid]['entity_id']: float(score) for nid, score in paper_scores.items()}

    # 2) compute affil scores
    affil_scores = defaultdict(float)
    for each_doc in docs:
      if not paper_affils.has_key(each_doc):
        continue
      # normalized score
      # count = float(len(author_affils[each_author]))

      score = paper_scores[each_doc]
      # directed affiliateship
      for each_affil in paper_affils[each_doc]:
        affil_scores[each_affil] += score

    return affil_scores



  def get_ranked_affils_by_authors(self, conf_name, year, age_relev, n_hops, alpha, exclude=[], expanded_year=[], expand_conf_year=[]):
    # # 0)
    # docs, citation_edges, paper_authors, paper_affils = self.get_paper_affils(conf_name, year, age_relev, exclude)
    # # run pagerank on author layer
    # graph = self.assemble_layers(docs, citation_edges,
    #                None, None, None, None,
    #                None, None, None)

    # paper_scores = rank_single_layer_nodes(graph, alpha=alpha)
    # paper_scores = {graph.node[nid]['entity_id']: float(score) for nid, score in paper_scores.items()}


    # # computes affil scores
    # author_scores = defaultdict()
    # for each_doc in docs:
    #   if not paper_authors.has_key(each_doc):
    #     continue
    #   # normalized score
    #   # count = float(len(author_affils[each_author]))

    #   score = paper_scores[each_doc]
    #   # directed affiliateship
    #   for each_author in paper_authors[each_doc]:
    #     try:
    #       author_scores[each_author] += score
    #     except:
    #       author_scores[each_author] = score




    # 1) page layer -> author layer
    authors, author_author_edges, coauthor_edges, author_affils = self.get_projected_author_layer(conf_name, year, age_relev, exclude, expanded_year, expand_conf_year)

    # 1) run pagerank on author layer
    graph = self.assemble_layers(None, None,
                   authors, author_author_edges, None, None,
                   None, None, None)

    author_scores = rank_single_layer_nodes(graph, alpha=alpha)
    author_scores = {graph.node[nid]['entity_id']: float(score) for nid, score in author_scores.items()}


    # graph2 = self.assemble_layers(None, None,
    #                authors, None, coauthor_edges, None,
    #                None, None, None)

    # auth_mapping = {graph2.node[x]['entity_id']:x for x in graph2.nodes()}
    # W = nx.stochastic_graph(graph2, weight='weight') # create a copy in (right) stochastic form

    # import pdb;pdb.set_trace()
    # 2) compute affil scores
    affil_scores = defaultdict(float)
    for each_author in authors:
      if not author_affils.has_key(each_author):
        continue
      # normalized score
      # count = float(len(author_affils[each_author]))

      score = author_scores[each_author]
      # directed affiliateship
      for each_affil in author_affils[each_author]:
        affil_scores[each_affil] += score


      # # one-hop affiliateship (having coathor belong to that affil)
      # for k, v in W[auth_mapping[each_author]].iteritems(): # neighbors of each_author
      #   nbor = graph2.node[k]['entity_id']
      #   if author_affils.has_key(nbor):
      #     for each_affil2 in author_affils[nbor]:
      #       if not each_affil2 in author_affils[each_author]:
      #         try:
      #           affil_scores[each_affil2] += score * v['weight']
      #         except:
      #           affil_scores[each_affil2] = score * v['weight']

    return affil_scores


  def build_projected_layers(self, conf_name, year, age_relev, n_hops, alpha, exclude=[], expanded_year=[]):
    # 1) page layer -> author layer
    authors, author_author_edges, coauthor_edges, author_affils = self.get_projected_author_layer(conf_name, year, age_relev, exclude, expanded_year)

    # run pagerank on author layer
    graph = self.assemble_layers(None, None,
                   authors, author_author_edges, None, None,
                   None, None, None)

    author_scores = rank_single_layer_nodes(graph, alpha=alpha)
    author_scores = {graph.node[nid]['entity_id']: float(score) for nid, score in author_scores.items()}


    # 2) author layer -> affil layer
    affil_affils = defaultdict()
    for author1, author2, _ in author_author_edges:
      if author_affils.has_key(author1) and author_affils.has_key(author2):
        affils1 = author_affils[author1]
        affils2 = author_affils[author2]

        score = author_scores[author1]
        for affil1 in affils1:
          for affil2 in affils2:
            if affil1 != affil2:
              if affil_affils.has_key(affil1):
                if affil_affils[affil1].has_key(affil2):
                  affil_affils[affil1][affil2] += score
                else:
                  affil_affils[affil1][affil2] = score
              else:
                affil_affils[affil1] = {affil2: score}

    affil_affil_edges = set()
    for k, v in affil_affils.iteritems():
      for each, weight in v.iteritems():
        # weight = np.log10(1.0 + weight)
        affil_affil_edges.add((k, each, weight))


    affils = [y for x in author_affils.values() for y in x]
    # graph = self.assemble_layers(None, None,
    #                None, None, None, None,
    #                affils, None, affil_affil_edges)


    author_affil_edges = [(k, y, 1.0) for k, v in author_affils.iteritems() for y in v]
    graph = self.assemble_layers(None, None,
                   authors, author_author_edges, None, None,
                   affils, author_affil_edges, affil_affil_edges)
    return graph


  def build_projected_layers2(self, conf_name, year, age_relev, n_hops, alpha, exclude=[], expanded_year=[], expand_conf_year=[]):
    # # 0)
    # docs, citation_edges, paper_authors, paper_affils = self.get_paper_affils(conf_name, year, age_relev, exclude)
    # # run pagerank on author layer
    # graph = self.assemble_layers(docs, citation_edges,
    #                None, None, None, None,
    #                None, None, None)

    # paper_scores = rank_single_layer_nodes(graph, alpha=alpha)
    # paper_scores = {graph.node[nid]['entity_id']: float(score) for nid, score in paper_scores.items()}


    # # computes affil scores
    # author_scores = defaultdict()
    # for each_doc in docs:
    #   if not paper_authors.has_key(each_doc):
    #     continue
    #   # normalized score
    #   # count = float(len(author_affils[each_author]))

    #   score = paper_scores[each_doc]
    #   # directed affiliateship
    #   for each_author in paper_authors[each_doc]:
    #     try:
    #       author_scores[each_author] += score
    #     except:
    #       author_scores[each_author] = score



    # 1) page layer -> author layer
    authors, author_author_edges, coauthor_edges, author_affils = self.get_projected_author_layer(conf_name, year, age_relev, exclude, expanded_year, expand_conf_year)

    # run pagerank on author layer
    graph = self.assemble_layers(None, None,
                   authors, author_author_edges, None, None,
                   None, None, None)


    author_scores = rank_single_layer_nodes(graph, alpha=alpha)
    author_scores = {graph.node[nid]['entity_id']: float(score) for nid, score in author_scores.items()}


    # 1) computes affil scores
    affil_scores = defaultdict(float)
    for each_author in authors:
      if not author_affils.has_key(each_author):
        continue
      # normalized score
      # count = float(len(author_affils[each_author]))

      score = author_scores[each_author]
      # directed affiliateship
      for each_affil in author_affils[each_author]:
        affil_scores[each_affil] += score



    # 2) author layer -> affil layer
    affil_affils = defaultdict()
    for author1, author2, _ in author_author_edges:
      if author_affils.has_key(author1) and author_affils.has_key(author2):
        affils1 = author_affils[author1]
        affils2 = author_affils[author2]

        score = author_scores[author1]
        for affil1 in affils1:
          for affil2 in affils2:
            if affil1 != affil2:
              if affil_affils.has_key(affil1):
                if affil_affils[affil1].has_key(affil2):
                  affil_affils[affil1][affil2] += score
                else:
                  affil_affils[affil1][affil2] = score
              else:
                affil_affils[affil1] = {affil2: score}

    affil_affil_edges = set()
    for k, v in affil_affils.iteritems():
      for each, weight in v.iteritems():
        # weight = np.log10(1.0 + weight)
        affil_affil_edges.add((k, each, weight))


    affils = [y for x in author_affils.values() for y in x]
    # graph = self.assemble_layers(None, None,
    #                None, None, None, None,
    #                affils, None, affil_affil_edges, affil_scores)


    author_affil_edges = [(k, y, 1.0) for k, v in author_affils.iteritems() for y in v]
    graph = self.assemble_layers(None, None,
                   authors, author_author_edges, None, None,
                   affils, author_affil_edges, affil_affil_edges, affil_scores, author_scores)



    # docs, citation_edges, _, paper_affils = self.get_paper_affils(conf_name, year, age_relev, exclude)

    # graph = self.assemble_layers(docs, citation_edges,
    #                authors, author_author_edges, None, None,
    #                affils, author_affil_edges, affil_affil_edges, affil_scores)

    return graph



  def build_projected_author_layer(self, conf_name, year, age_relev, n_hops, alpha, exclude=[], expanded_year=[]):
    # 1) page layer -> author layer
    authors, author_author_edges, _, author_affils = self.get_projected_author_layer(conf_name, year, age_relev, exclude, expanded_year)
    affils = set([y for x in author_affils.values() for y in x])
    # author_affil_edges = [(k, y, 1.0) for k, v in author_affils.iteritems() for y in v]
    author_authors = defaultdict(dict)
    for k, v, w in author_author_edges:
      author_authors[k][v] = w

    # graph = self.assemble_layers(None, None,
    #                authors, author_author_edges, None, None,
    #                affils, author_affil_edges, None, None, None)

    # return graph

    return authors, author_authors, affils, author_affils


  def build_stat_layer(self, conf_name, year, age_relev, n_hops, exclude=[], expanded_year=[]):
    """
    get the distribution info of layers and build co-author layer
    """

    authors, coauthor_edges, author_affils, author_per_paper_dist, author_ncites, author_npubs = self.get_paper_author_dist(conf_name, year, age_relev, exclude, expanded_year)

    author_graph = self.assemble_layers(None, None,
                   authors, None, coauthor_edges, None,
                   None, None, None, None, None)

    # import pdb;pdb.set_trace()
    # x = sorted(author_npubs.items(), key=lambda d:d[1], reverse=True)
    # ezplot(range(len(zip(*x)[0])), zip(*x)[1], title='author - citations (%s)'%', '.join(year) if hasattr(year, '__iter__') else year, xlabel='rank of authors', ylabel='citation')
    # ezplot(author_per_paper_dist.keys(), np.array(author_per_paper_dist.values())/sum(author_per_paper_dist.values()), title='# of authors per paper distribution (%s)'%', '.join(year) if hasattr(year, '__iter__') else year, xlabel='# of authors per paper', ylabel='percentage')
    author_per_paper_dist = normalize(author_per_paper_dist)
    author_scores = normalize(author_npubs)

    return author_graph, author_affils, author_per_paper_dist, author_scores




  def simple_author_rating(self, conf_name, year, expand_year=[]):
    """
    rating authors based on publications
    """
    author_scores = defaultdict(float)
    author_affils = defaultdict(set)
    pub_records, _, __ = get_selected_expand_pubs(conf_name, year, _type='selected')

    # expand docs set by getting more papers accepted by the targeted conference
    if expand_year:
      conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf_name, limit=1)[0]
      expand_records, _, __ = get_selected_expand_pubs(conf_id, expand_year, _type='expanded')
      pub_records.update(expand_records)
      print 'expanded %s papers.'%len(expand_records)

    for _, record in pub_records.iteritems():
      score = 1.0
      # score = 1.0 / len(record['author'])
      for author_id, affil_ids in record['author'].iteritems():
        author_scores[author_id] += score
        author_affils[author_id].update(affil_ids)

    author_scores = OrderedDict(sorted(author_scores.iteritems(), key=lambda d:d[1], reverse=True))

    return author_scores, author_affils


  def simple_affil_rating(self, conf_name, year, expand_year=[]):
    """
    rating affils based on publications
    """

    affil_scores = defaultdict(float)
    pub_records, _, __ = get_selected_expand_pubs(conf_name, year, _type='selected')

    # expand docs set by getting more papers accepted by the targeted conference
    if expand_year:
      conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf_name, limit=1)[0]
      expand_records, _, __ = get_selected_expand_pubs(conf_id, expand_year, _type='expanded')
      pub_records.update(expand_records)
      print 'expanded %s papers.'%len(expand_records)

    for _, record in pub_records.iteritems():
      affil_ids = set([x for _, affil_ids in record['author'].iteritems() for x in affil_ids])
      for each in affil_ids:
        affil_scores[each] += 1.0 # count # of papers


    affil_scores = OrderedDict(sorted(affil_scores.iteritems(), key=lambda d:d[1], reverse=True))

    return affil_scores


  def get_year_author_rating(self, conf_name, force=False):
    if force:
      author_affils = defaultdict()
      year_author_rating = defaultdict()
      for year in range(2001, 2011):
        author_scores, tmp_author_affils = self.simple_author_rating(conf_name, year=[], expand_year=str(year))
        year_author_rating[str(year)] = author_scores
        author_affils.update(tmp_author_affils)

      for year in range(2011, 2016):
        author_scores, tmp_author_affils = self.simple_author_rating(conf_name, str(year))
        year_author_rating[str(year)] = author_scores
        author_affils.update(tmp_author_affils)

      # save it
      with open('%s_year_author_rating.json'%conf_name, 'w') as fp:
        json.dump(year_author_rating, fp)
        fp.close()

      with open('%s_author_affils.json'%conf_name, 'w') as fp:
        author_affils = {x:list(y) for x, y in author_affils.items()}
        json.dump(author_affils, fp)
        fp.close()


    else:
      with open('%s_year_author_rating.json'%conf_name, 'r') as fp:
        year_author_rating = json.load(fp)
        fp.close()

      # with open('%s_author_affils.json'%conf_name, 'r') as fp:
      #   import pdb;pdb.set_trace()
      #   author_affils = json.load(fp)
      #   fp.close()
      author_affils = {}

    # check stableness of each year's top authors
    print "stableness of each year's top authors:"
    for year in range(2001, 2015):
      cur_year_toplist = year_author_rating[str(year)].keys()
      next_year_toplist = year_author_rating[str(year+1)].keys()
      comm_authors = set(cur_year_toplist) & set(next_year_toplist)
      print "%s-%s # of comm authors: %s" % (year, year+1, len(comm_authors))

    print "# of authors each year:"
    for year in range(2001, 2015):
      print "%s # of authors: %s" % (year, len(year_author_rating[str(year)].keys()))


    # generate a watching list of active authors based on past 5 years' records
    watching_list = set()
    for year in range(2010, 2015):
      watching_list.update(year_author_rating[str(year)].keys())

    return year_author_rating, watching_list, author_affils



  def get_year_affil_rating(self, conf_name, force=False):
    if force:
      year_affil_rating = defaultdict()
      for year in range(2001, 2011):
        year_affil_rating[str(year)] = self.simple_affil_rating(conf_name, year=[], expand_year=str(year))

      for year in range(2011, 2016):
        year_affil_rating[str(year)] = self.simple_affil_rating(conf_name, str(year))

      # save it
      with open('%s_year_affil_rating.json'%conf_name, 'w') as fp:
        json.dump(year_affil_rating, fp)
        fp.close()

    else:
      with open('%s_year_affil_rating.json'%conf_name, 'r') as fp:
        year_affil_rating = json.load(fp)
        fp.close()


    # check stableness of each year's top affils
    print "stableness of each year's top affils:"
    for year in range(2001, 2015):
      cur_year_toplist = year_affil_rating[str(year)].keys()
      next_year_toplist = year_affil_rating[str(year+1)].keys()
      comm_affils = set(cur_year_toplist) & set(next_year_toplist)
      print "%s-%s # of comm affils: %s" % (year, year+1, len(comm_affils))

    print "# of affils each year:"
    for year in range(2001, 2015):
      print "%s # of affils: %s" % (year, len(year_affil_rating[str(year)].keys()))


    # generate a watching list of active affils based on past 5 years' records
    watching_list = set()
    for year in range(2010, 2015):
      watching_list.update(year_affil_rating[str(year)].keys())

    return year_affil_rating, watching_list


  def review_trends(self, year_rating, watching_list, plot=False, _type='affil'):
    year_trends = defaultdict()
    for each in watching_list:
      year_trends[each] = {}
      for year, ratings in year_rating.iteritems():
        year_trends[each][year] = ratings[each] \
                                if each in ratings else .0

    # sort it
    year_trends = OrderedDict(sorted(year_trends.iteritems(), key=lambda d:sum(d[1].values()), reverse=True))

    # plot it
    if plot:
      for each, trends in year_trends.items()[:20]:
        plt.figure()
        plt.title('KDD - %s'%each)
        x, y = zip(*(sorted(trends.iteritems(), key=lambda d:d[0])))
        plt.plot(x, y, 'o--', label='publishing trends')
        plt.savefig('img/%s_trends/KDD-%s.png'%(_type, each))
        # plt.show()

    return year_trends




  def pred_trends(self, year_trends, end_year='2015', scalar=.4):
    """
    predicate trends and variance based on historical records.
    """

    count_correct = 0
    new_trends = defaultdict()

    for stuff, trends in year_trends.items():
      filtered_trends = {year:score for year, score in trends.iteritems() if year <= end_year}

      years, scores = zip(*(sorted(filtered_trends.iteritems(), key=lambda d:d[0])))

      # find the first one which is larger than 0
      try:
        start_idx = (np.array(scores) > 0).tolist().index(True)
      except:
        continue

      start_idx = start_idx - 1 if start_idx > 0 else start_idx # works better in practice
      scores = scores[start_idx:]

      # if np.sum(scores[-5:]) < 2:
      #   continue

      # variance or fluctuation

      sigma = np.mean([abs(scores[i] - scores[i + 1]) for i in range(len(scores) - 1) if not scores[i] == scores[i + 1]]) if len(scores) > 1 else .0

      # trend, 1 stands for going up, -1 stands for going down, 0 stands for keeping stable

      # if current point is above the mean point, goes down, if below, goes up, otherwise, keeps stable
      # works well in practice!

      if abs(scores[-1] - np.mean(scores)) <= .4 * sigma:
        trend = .0
      elif scores[-1] < np.mean(scores):
        # trend = 1.0
        trend = min(abs(scores[-1] - np.mean(scores)) / sigma, 1.0)
      else:
        # trend = -1.0
        trend = -min(abs(scores[-1] - np.mean(scores)) / sigma, 1.0)


      # if trend > 0 and scores[-1] < year_trends[stuff][str(int(end_year)+1)]:
      #   count_correct += 1
      # elif trend < 0 and scores[-1] > year_trends[stuff][str(int(end_year)+1)]:
      #   count_correct += 1
      # elif trend == 0 and scores[-1] == year_trends[stuff][str(int(end_year)+1)]:
      #   count_correct += 1
      # else:
      #   # import pdb;pdb.set_trace()
      #   pass

        # # test, assuming we can predict the trends perfectly
        # if scores[-1] < year_trends[stuff][str(int(end_year)+1)]:
        #   trend = 1
        # elif scores[-1] > year_trends[stuff][str(int(end_year)+1)]:
        #   trend = -1
        # else:
        #   trend = 0

      # scalar .4
      new_trends[stuff] = sigmoid(scalar * trend * sigma / np.mean(scores)) # compute the factor of changes

    # print "precision of trend pred: %s/%s" % (count_correct, len(new_trends))

    return new_trends




  def calc_temporal_scores(self, scores, pred_trends, scalar=1.0):
    print "# of comm authors: %s" % len(set(scores.keys()) & set(pred_trends.keys()))
    # scalar = 1.0

    # normalize
    # scores = range_normalize(scores)
    # pred_trends = range_normalize(pred_trends)

    # import pdb;pdb.set_trace()
    temporal_scores = defaultdict()
    for each, trend in pred_trends.iteritems():
      if not each in scores:
        continue

      # temporal_scores[each] = scores[each] + scalar * trend
      temporal_scores[each] = scores[each] * (1 + trend)

    temporal_scores = OrderedDict(sorted(temporal_scores.iteritems(), key=lambda d:d[1], reverse=True))

    return temporal_scores




  def rate_affil_by_author(self, author_scores, author_affils):
    affil_scores = defaultdict(float)

    for author, score in author_scores.iteritems():
      if not author in author_affils:
        continue

      for each_affil in author_affils[author]:
        affil_scores[each_affil] += score

    return affil_scores


  def rate_author_on_history(self, year, year_author_rating):
    author_scores = defaultdict(float)
    for yr, records in year_author_rating.iteritems():
      if not yr in year:
        continue

      for author, score in records.iteritems():
        author_scores[author] += score

    # for author, records in author_year_trends.iteritems():
    #   author_scores[author] = sum([score for yr, score in records.iteritems() if int(yr) in year])

    for author, score in author_scores.iteritems():
      author_scores[author] = score / float(len(year))

    return author_scores



  def rate_projected_authors(self, conf_name, year, age_relev, n_hops, alpha, exclude=[], expanded_year=[], expand_conf_year=[]):
    # 1) page layer -> author layer
    authors, author_author_edges, coauthor_edges, author_affils = self.get_projected_author_layer(conf_name, year, age_relev, exclude, expanded_year, expand_conf_year)

    # 1) run pagerank on author citation layer
    graph = self.assemble_layers(None, None,
                   authors, author_author_edges, None, None,
                   None, None, None)

    cite_author_scores = rank_single_layer_nodes(graph, alpha=alpha)

    author_scores = cite_author_scores

    # # 2) run pagerank on author coauthorship layer
    # graph = self.assemble_layers(None, None,
    #                authors, None, coauthor_edges, None,
    #                None, None, None)

    # coauth_author_scores = rank_single_layer_nodes(graph, alpha=alpha)

    # beta = 1.0

    # vals = (beta * np.array(cite_author_scores.values()) + \
    #       (1 - beta) * np.array(coauth_author_scores.values())).tolist()
    # author_scores = dict(zip(cite_author_scores.keys(), vals))

    author_scores = {graph.node[nid]['entity_id']: float(score) for nid, score in author_scores.items()}

    return author_scores, author_affils



  # For SupervisedSearcher approach

  def count_for_affils(self, conf_name, year=[], expand_year=[], online_search=False):
    """
    rating affils based on publications
    """

    affil_scores = defaultdict(float)
    affil_authors = defaultdict(set)
    affil_npapers = defaultdict(float)
    pub_records = defaultdict()

    if year:
      records, _, __ = get_selected_expand_pubs(conf_name, year, _type='selected', online_search=online_search)
      pub_records.update(records)



    # expand docs set by getting more papers accepted by the targeted or related conference
    if expand_year:
      conf_id = db.select("id", "confs", where="abbr_name='%s'"%conf_name, limit=1)[0]
      expand_records, _, __ = get_selected_expand_pubs(conf_id, expand_year, _type='expanded', online_search=online_search)
      pub_records.update(expand_records)
      print 'expanded %s papers from %s.' % (len(expand_records), conf_name)


    for _, record in pub_records.iteritems():
      score1 = 1.0 / len(record['author'])
      for author, affil_ids in record['author'].iteritems():
        score2 = score1 / len(affil_ids)
        for each in affil_ids:
          affil_authors[each].add(author)
          affil_scores[each] += score2


      affil_ids = set([x for _, affil_ids in record['author'].iteritems() for x in affil_ids])
      for each in affil_ids:
        affil_npapers[each] += 1.0 # count # of papers



    return affil_scores, affil_npapers, affil_authors




  def get_features(self, records, feature_list, year, wnd, prefix=''):
    affil_features = defaultdict(dict)

    for each_year in year:
      if not str(each_year) in records:
        continue

      for each_feature in feature_list:
        if not each_feature in records[str(each_year)]:
          continue

        for affil, val in records[str(each_year)][each_feature].iteritems():
            try:
              if each_feature == '%snauthors'%prefix:
                affil_features[affil]["%s_y%s"%(each_feature, wnd)].update(val)

              else:
                affil_features[affil]["%s_y%s"%(each_feature, wnd)] += val

            except:
              affil_features[affil]["%s_y%s"%(each_feature, wnd)] = val


    if '%snauthors'%prefix in feature_list:
      for affil in affil_features.keys():
        try:
          affil_features[affil]["%snauthors_y%s"%(prefix, wnd)] = float(len(affil_features[affil]["%snauthors_y%s"%(prefix, wnd)]))
        except Exception,e:
          print e

    return affil_features



  def get_all_metadata(self, conf_name, year, expanded_year=[], expand_conf_year=[], force=True):
    if force:
      year_affil_records = defaultdict(dict)
      year_affil_records_otherconfs = defaultdict(dict)



      for each_year in year:
        affil_scores, affil_npapers, affil_authors = self.count_for_affils(conf_name, str(each_year))

        year_affil_records[str(each_year)] = {'score':affil_scores, 'npapers':affil_npapers, 'nauthors':affil_authors}


      for each_year in expanded_year:
          affil_scores, affil_npapers, affil_authors = self.count_for_affils(conf_name, [], str(each_year))

          year_affil_records[str(each_year)] = {'score':affil_scores, 'npapers':affil_npapers, 'nauthors':affil_authors}


      # data from other related conferences

      for conf, conf_years in expand_conf_year:
        for each_year in conf_years:

          affil_scores, affil_npapers, affil_authors = self.count_for_affils(conf, [], str(each_year))

          year_affil_records_otherconfs[conf][str(each_year)] = {'%s_score'%conf:affil_scores, '%s_npapers'%conf:affil_npapers, '%s_nauthors'%conf:affil_authors}



      # for each affil, we assign a meta record which contains info such as
      # # of active authors in past 5 years, # of papers in past 5 years, ...

      training_records = []
      testing_records = defaultdict()


      year_windows = [1, 2, 5] # past 1, 2, 5, all years

      year_list = expanded_year + year

      start_idx = 1
      # start_idx = max(year_windows)

      for idx in range(start_idx, len(year_list)+1):
        # each loop is a year

        record = defaultdict(dict)

        # get features
        for wnd in year_windows:
          affil_features = self.get_features(year_affil_records, ['npapers', 'nauthors', 'score'], range(int(year_list[idx-1]), int(year_list[0])-1, -1)[:wnd], wnd)
          for k, v in affil_features.iteritems():
            record[k].update(v)



          # get features from other confs
          for conf, vals in year_affil_records_otherconfs.iteritems():
            affil_features = self.get_features(vals, ['%s_npapers'%conf, '%s_nauthors'%conf, '%s_score'%conf], range(int(year_list[idx-1]), int(year_list[0])-1, -1)[:wnd], wnd, '%s_'%conf)
            for k, v in affil_features.iteritems():
              if k in record:
                record[k].update(v)



        if idx == len(year_list):
          testing_records = deepcopy(record)

        else:
          # get target
          affil_target = year_affil_records[str(year_list[idx])]['score']
          for affil in record.keys():
            record[affil]['score'] = affil_target[affil] if affil in affil_target else .0
            record[affil]['year_idx'] = idx

          # for k, v in affil_target.iteritems():
          #   record[k]['score'] = v
          training_records.extend(record.values())


      with open("training_records.json", "w") as fp:
        json.dump(training_records, fp)
        fp.close()

      with open("testing_records.json", "w") as fp:
        json.dump(testing_records, fp)
        fp.close()

    else:
      with open("training_records.json", "r") as fp:
        training_records = json.load(fp)
        fp.close()

      with open("testing_records.json", "r") as fp:
        testing_records = json.load(fp)
        fp.close()


    return training_records, testing_records


  def format_data(self, meta_records):
    df = pd.DataFrame.from_dict(meta_records)
    df = df.fillna(0) # fill nan

    # df = df - df.mean()


    if 'score' in df.columns: # training data
      y = df.score.values
      train = df.drop(['score', 'year_idx'], axis=1)
      return train, y
    else: # testing data
        return df



  # For Learning to rank
  def generate_training_data(self, meta_records, save_file='formated_training.txt'):
    """
    The file format for the training data (also testing/validation data)
    is the same as for SVM-Rank. This is also the format used in LETOR datasets.
    Each of the following lines represents one training example and
    is of the following format:
    <line> .=. <target> qid:<qid> <feature>:<value> <feature>:<value> ... <feature>:<value> # <info>
    <target> .=. <positive integer>
    <qid> .=. <positive integer>
    <feature> .=. <positive integer>
    <value> .=. <float>
    <info> .=. <string>


    Here's an example: (taken from the SVM-Rank website). Note that everything after "#" are ignored.
    3 qid:1 1:1 2:1 3:0 4:0.2 5:0 # 1A
    2 qid:1 1:0 2:0 3:1 4:0.1 5:1 # 1B
    1 qid:1 1:0 2:1 3:0 4:0.4 5:0 # 1C
    """

    # meta_records: [{'year_idx':idx, 'score':score, feature1:val1, },]
    score_scalar = 10.0
    try:
      with open(save_file, 'w') as fp:
        df = pd.DataFrame.from_dict(meta_records)
        df = df.fillna(0) # fill nan

        for i, row in df.iterrows(): # each row
          qid = int(row['year_idx'])
          score = int(round(row['score'] * score_scalar))

          features = ""
          ii = 1
          for k, v in row.iteritems():
            if k == 'score' or k == 'year_idx':
              continue

            features += " %s:%s" % (ii, v)
            ii += 1

          line = "%s qid:%s" % (score, qid) + features + '\n'

          fp.writelines(line)


    except Exception, e:
      print e
      exit()

    fp.close()


  def generate_testing_data(self, meta_records, save_file='formated_testing.txt'):
    try:
      with open(save_file, 'w') as fp:
        df = pd.DataFrame.from_dict(meta_records)
        df = df.fillna(0) # fill nan
        qid = 1
        score = 0

        for i, row in df.iterrows(): # each row
          features = ""
          ii = 1
          for k, v in row.iteritems():
            if k == 'score' or k == 'year_idx':
              continue

            features += " %s:%s" % (ii, v)
            ii += 1

          line = "%s qid:%s" % (score, qid) + features + '\n'

          fp.writelines(line)


    except Exception, e:
      print e
      exit()

    fp.close()


  def read_scorefile(self, file):
    scores = []
    try:
      with open(file, 'r') as fp:
        for line in fp:
          scores.append(float(line.strip('\n ')))

    except Exception, e:
      print e
      sys.exit()

    fp.close()
    return scores


def sigmoid(x):
  return 2 * np.exp(x) / (1 + np.exp(x)) - 1


def normalize(x):
  sums = sum(x.values())
  xx = {k:v/sums for k, v in x.iteritems()}
  return xx

def range_normalize(x):
  _range = max(x.values()) - min(x.values())

  vals = ((np.array(x.values()) - min(x.values())) / _range).tolist()
  xx = dict(zip(x.keys(), vals))

  return xx


def ezplot(x, y, **kwargs):
  plt.plot(x, y)
  if 'title' in kwargs:
    plt.title(kwargs['title'])

  if 'xlabel' in kwargs:
    plt.xlabel(kwargs['xlabel'])

  if 'ylabel' in kwargs:
    plt.ylabel(kwargs['ylabel'])

  plt.show()


if __name__ == '__main__':
  log.basicConfig(format='%(asctime)s [%(levelname)s] : %(message)s', level=log.INFO)
  mb = ModelBuilder()
  graph = mb.build_full_graph()

