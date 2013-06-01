#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Created on 2013-5-28

@author: Chine
'''

import os
import time
import threading
import signal
import random
import sys

from cola.core.mq import MessageQueue
from cola.core.mq.node import Node
from cola.core.bloomfilter import FileBloomFilter
from cola.core.rpc import ColaRPCServer, client_call
from cola.core.utils import get_ip, root_dir
from cola.core.errors import ConfigurationError
from cola.core.logs import get_logger

MAX_THREADS_SIZE = 10
TIME_SLEEP = 10
BUDGET_REQUIRE = 10

UNLIMIT_BLOOM_FILTER_CAPACITY = 10000

class JobLoader(object):
    def __init__(self, job, mq=None, logger=None, master=None, context=None):
        self.job = job
        self.mq = mq
        self.master = master
        self.logger = logger
        
        # If stop
        self.stopped = False
        
        self.ctx = context or self.job.context
        self.instances = max(min(self.ctx.job.instances, MAX_THREADS_SIZE), 1)
        self.size =self.ctx.job.size
        self.budget = 0
        
        # The execute unit
        self.executing = None
        
        # register signal
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
    def init_mq(self, rpc_server, nodes, local_node, loc, 
                verify_exists_hook=None, copies=1):
        mq_store_dir = os.path.join(loc, 'store')
        mq_backup_dir = os.path.join(loc, 'backup')
        if not os.path.exists(mq_store_dir):
            os.mkdir(mq_store_dir)
        if not os.path.exists(mq_backup_dir):
            os.mkdir(mq_backup_dir)
        mq_store = Node(mq_store_dir, verify_exists_hook=verify_exists_hook)
        mq_backup = Node(mq_backup_dir)
        
        # MQ relative
        self.mq = MessageQueue(
            nodes,
            local_node,
            rpc_server,
            mq_store,
            mq_backup,
            copies=copies
        )
        
    def stop(self):
        self.stopped = True
        
        if self.executing is not None:
            self.mq.put(self.executing)
        
        self.finish()
        
    def signal_handler(self, signum, frame):
        self.stop()
        
    def complete(self, obj):
        if self.logger is not None:
            self.logger.info('Finish %s' % obj)
        
        if self.ctx.job.size <= 0:
            return False
        
        self.executing = None
        if self.master is not None:
            return client_call(master, 'complete', obj)
        else:
            self.size -= 1
            # sth to log
            if self.size <= 0:
                self.stopped = True
            return self.stopped
            
    def finish(self):
        self.mq.shutdown()
        
    def _require_budget(self):
        if self.master is None or self.ctx.job.limits == 0:
            return
        
        if self.budget > 0:
            self.budget -= 1
            return
        
        while self.budget == 0 and not self.stopped:
            self.budget = client_call(self.master, 'require', BUDGET_REQUIRE)
            
    def _log(self, obj, err):
        if self.logger is not None:
            self.logger.info('Error when get bundle: %s' % obj)
            self.logger.exception(err)
            
        if self.job.debug:
            raise err
        
    def _execute(self, obj):
        if self.job.is_bundle:
            bundle = self.job.unit_cls(obj)
            urls = bundle.urls()
            
            try:
                
                while len(urls) > 0 or not self.stopped:
                    url = urls.pop(0)
                    
                    parser_cls = self.job.url_patterns.get_parser(url)
                    if parser_cls is not None:
                        self._require_budget()
                        next_urls, bundles = parser_cls(self.job.opener_cls, url).parse()
                        next_urls = list(self.job.url_patterns.matches(next_urls))
                        next_urls.extend(urls)
                        urls = next_urls
                        if bundles:
                            self.mq.put(bundles)
                            
            except Exception, e:
                self._log(obj, e)
                
        else:
            self._require_budget()
            
            try:
                
                parser_cls = self.job.url_patterns.get_parser(obj)
                if parser_cls is not None:
                    next_urls = parser_cls(self.job.opener_cls, obj).parse()
                    next_urls = list(self.job.url_patterns.matches(next_urls))
                    self.mq.put(next_urls)
                    
            except Exception, e:
                self._log(obj, e)
                    
            
        return self.complete(obj)
        
    def run(self):
        if self.job.login_hook is not None:
            if 'login' not in self.ctx.job or \
                not isinstance(self.ctx.job.login, list):
                raise ConfigurationError('If login_hook set, config files must contains `login`')
            kw = random.choice(self.ctx.job.login)
            self.job.login_hook(**kw)
        
        def _call():
            stopped = False
            while not self.stopped and not stopped:
                obj = self.mq.get()
                print 'start to get %s' % obj
                if obj is None:
                    time.sleep(TIME_SLEEP)
                    continue
                
                self.executing = obj
                stopped = self._execute(obj)
                
        try:
            threads = [threading.Thread(target=_call) for _ in range(self.instances)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            self.finish()

def create_rpc_server(job, context=None):
    ctx = context or job.context
    rpc_server = ColaRPCServer((get_ip(), ctx.job.port))
    thd = threading.Thread(target=rpc_server.serve_forever)
    thd.setDaemon(True)
    thd.start()
    return rpc_server

def load_job(path, master=None):
    if not os.path.exists(path):
        raise ValueError('Job definition does not exist.')
        
    dir_, name = os.path.split(path)
    if os.path.isfile(path):
        name = name.rstrip('.py')
    sys.path.insert(0, dir_)
    job_module = __import__(name)
    job = job_module.get_job()
    
    holder = os.path.join(root_dir(), 'worker', job.name.replace(' ', '_'))
    mq_holder = os.path.join(holder, 'mq')
    if not os.path.exists(mq_holder):
        os.makedirs(mq_holder)
    
    # Logger
    logger = get_logger(os.path.join(holder, 'job.log'))
    
    local_node = '%s:%s' % (get_ip(), job.context.job.port)
    nodes = [local_node]
    if master is not None:
        nodes = client_call(master, 'get_nodes')
    
    # Bloom filter file
    bloom_filter_file = os.path.join(holder, 'bloomfilter')
    if job.context.job.size > 0:
        bloom_filter_size = job.context.job.size*2
    else:
        bloom_filter_size = UNLIMIT_BLOOM_FILTER_CAPACITY
    bloom_filter_hook = FileBloomFilter(bloom_filter_file, bloom_filter_size)
    
    rpc_server = create_rpc_server(job)
    loader = JobLoader(job, logger=logger, master=master)
    loader.init_mq(rpc_server, nodes, local_node, mq_holder, 
                   verify_exists_hook=bloom_filter_hook,
                   copies=2 if master else 1)
    
    if master is None:
        loader.mq.put(job.starts)
        loader.run()
        rpc_server.shutdown()
    else:
        try:
            _start_to_run = False
            def _run():
                _start_to_run = True
                loader.run()
            rpc_server.register_function(_run, name='run')
            rpc_server.register_function(loader.stop, name='stop')
            
            client_call(master, 'ready', local_node)
            
            # If master does not get ready
            while not _start_to_run: pass
        finally:
            rpc_server.shutdown()
            
if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise ValueError('Worker job loader need at least 1 parameters.')
    
    path = sys.argv[1]
    master = None
    if len(sys.argv) > 2:
        master = sys.argv[2]
    load_job(path, master=master)