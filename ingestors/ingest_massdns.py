#!/usr/bin/env python
# Elasticsearch Recon Ingestion Scripts (ERIS) - Developed by Acidvegas (https://git.acid.vegas/eris)

# Massdns Log File Ingestion:
#
# This script takes JSON formatted massdns logs and indexes them into Elasticsearch.
#
# Saving my "typical" massdns command here for reference to myself:
#   python $HOME/massdns/scripts/ptr.py | massdns -r $HOME/massdns/nameservers.txt -t PTR --filter NOERROR -o J -w $HOME/massdns/ptr.json

import argparse
import logging
import os
import time

try:
    from elasticsearch import Elasticsearch, helpers
except ImportError:
    raise ImportError('Missing required \'elasticsearch\' library. (pip install elasticsearch)')


# Setting up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%m/%d %I:%M:%S')


class ElasticIndexer:
    def __init__(self, es_host: str, es_port: int, es_user: str, es_password: str, es_api_key: str, es_index: str, dry_run: bool = False, self_signed: bool = False, retries: int = 10, timeout: int = 30):
        '''
        Initialize the Elastic Search indexer.

        :param es_host: Elasticsearch host(s)
        :param es_port: Elasticsearch port
        :param es_user: Elasticsearch username
        :param es_password: Elasticsearch password
        :param es_api_key: Elasticsearch API Key
        :param es_index: Elasticsearch index name
        :param dry_run: If True, do not initialize Elasticsearch client
        :param self_signed: If True, do not verify SSL certificates
        :param retries: Number of times to retry indexing a batch before failing
        :param timeout: Number of seconds to wait before retrying a batch
        '''

        self.dry_run = dry_run
        self.es = None
        self.es_index = es_index

        if not dry_run:
            es_config = {
                'hosts': [f'{es_host}:{es_port}'],
                'verify_certs': self_signed,
                'ssl_show_warn': self_signed,
                'request_timeout': timeout,
                'max_retries': retries,
                'retry_on_timeout': True,
                'sniff_on_start': False,
                'sniff_on_node_failure': True,
                'min_delay_between_sniffing': 60
            }

            if es_api_key:
                es_config['headers'] = {'Authorization': f'ApiKey {es_api_key}'}
            else:
                es_config['basic_auth'] = (es_user, es_password)
                
            # Patching the Elasticsearch client to fix a bug with sniffing (https://github.com/elastic/elasticsearch-py/issues/2005#issuecomment-1645641960)
            import sniff_patch
            self.es = sniff_patch.init_elasticsearch(**es_config)

            # Remove the above and uncomment the below if the bug is fixed in the Elasticsearch client:
            #self.es = Elasticsearch(**es_config)


    def create_index(self, shards: int = 1, replicas: int = 1):
        '''
        Create the Elasticsearch index with the defined mapping.
        
        :param shards: Number of shards for the index
        :param replicas: Number of replicas for the index
        '''

        mapping = {
            'settings': {
                'number_of_shards': shards,
                'number_of_replicas': replicas
            },
            'mappings': {
                'properties': {
                    'ip':     { 'type': 'ip' },
                    'name':   { 'type': 'keyword' },
                    'record': { 'type': 'text', 'fields': { 'keyword': { 'type': 'keyword', 'ignore_above': 256 } } },
                    'seen':   { 'type': 'date' }
                }
            }
        }

        if not self.es.indices.exists(index=self.es_index):
            response = self.es.indices.create(index=self.es_index, body=mapping)
            
            if response.get('acknowledged') and response.get('shards_acknowledged'):
                logging.info(f'Index \'{self.es_index}\' successfully created.')
            else:
                raise Exception(f'Failed to create index. ({response})')

        else:
            logging.warning(f'Index \'{self.es_index}\' already exists.')


    def get_cluster_health(self) -> dict:
        '''Get the health of the Elasticsearch cluster.'''

        return self.es.cluster.health()
    

    def get_cluster_size(self) -> int:
        '''Get the number of nodes in the Elasticsearch cluster.'''

        cluster_stats = self.es.cluster.stats()
        number_of_nodes = cluster_stats['nodes']['count']['total']

        return number_of_nodes


    def process_file(self, file_path: str, watch: bool = False, chunk: dict = {}):
        '''
        Read and index PTR records in batches to Elasticsearch, handling large volumes efficiently.

        :param file_path: Path to the Masscan log file
        :param watch: If True, input file will be watched for new lines and indexed in real time\
        :param chunk: Chunk configuration for indexing in batches

        Example PTR record:
        0.6.229.47.in-addr.arpa. PTR 047-229-006-000.res.spectrum.com.
        0.6.228.75.in-addr.arpa. PTR 0.sub-75-228-6.myvzw.com.
        0.6.207.73.in-addr.arpa. PTR c-73-207-6-0.hsd1.ga.comcast.net.
        0.6.212.173.in-addr.arpa. PTR 173-212-6-0.cpe.surry.net.
        0.6.201.133.in-addr.arpa. PTR flh2-133-201-6-0.tky.mesh.ad.jp.

        Will be indexed as:
        {
            "ip": "47.229.6.0",
            "record": "047-229-006-000.res.spectrum.com.",
            "seen": "2021-06-30T18:31:00Z"
        }
        '''

        count = 0
        records = []

        with open(file_path, 'r') as file:
            for line in (file := follow(file) if watch else file):
                line = line.strip()

                if not line:
                    continue

                parts = line.split()

                if len(parts) < 3:
                    raise ValueError(f'Invalid PTR record: {line}')
                
                name, record_type, data = parts[0].rstrip('.'), parts[1], ' '.join(parts[2:]).rstrip('.')

                if record_type != 'PTR':
                    continue

                # Let's not index the PTR record if it's the same as the in-addr.arpa domain
                if data == name:
                    continue

                ip = '.'.join(name.replace('.in-addr.arpa', '').split('.')[::-1])
                
                source = {
                    'ip': ip,
                    'record': data,
                    'seen': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                }
                
                if self.dry_run:
                    print(source)
                else:
                    struct = {'_index': self.es_index, '_source': source}
                    records.append(struct)
                    count += 1
                    if len(records) >= chunk['batch']:
                        self.bulk_index(records, file_path, chunk, count)
                        records = []

        if records:
           self.bulk_index(records, file_path, chunk, count)


    def bulk_index(self, documents: list, file_path: str, chunk: dict, count: int):
        '''
        Index a batch of documents to Elasticsearch.
        
        :param documents: List of documents to index
        :param file_path: Path to the file being indexed
        :param chunk: Chunk configuration
        :param count: Total number of records processed
        '''
        
        remaining_documents = documents

        parallel_bulk_config = {
            'client': self.es,
            'chunk_size': chunk['size'],
            'max_chunk_bytes': chunk['max_size'],
            'thread_count': chunk['threads'],
            'queue_size': 2
        }

        while remaining_documents:
            failed_documents = []

            try:
                for success, response in helpers.parallel_bulk(actions=remaining_documents, **parallel_bulk_config):
                    if not success:
                        failed_documents.append(response)

                if not failed_documents:
                    ingested = parallel_bulk_config['chunk_size'] * parallel_bulk_config['thread_count']
                    logging.info(f'Successfully indexed {ingested:,} ({count:,} processed) records to {self.es_index} from {file_path}')
                    break

                else:
                    logging.warning(f'Failed to index {len(failed_documents):,} failed documents! Retrying...')
                    remaining_documents = failed_documents
            except Exception as e:
                logging.error(f'Failed to index documents! ({e})')
                time.sleep(30)


def follow(file) -> str:
    '''
    Generator function that yields new lines in a file in real time.
    
    :param file: File object to read from
    '''

    file.seek(0,2) # Go to the end of the file

    while True:
        line = file.readline()

        if not line:
            time.sleep(0.1)
            continue

        yield line


def main():
    '''Main function when running this script directly.'''

    parser = argparse.ArgumentParser(description='Index data into Elasticsearch.')
    
    # General arguments
    parser.add_argument('input_path', help='Path to the input file or directory') # Required
    parser.add_argument('--dry-run', action='store_true', help='Dry run (do not index records to Elasticsearch)')
    parser.add_argument('--watch', action='store_true', help='Watch the input file for new lines and index them in real time')
    
    # Elasticsearch arguments
    parser.add_argument('--host', default='localhost', help='Elasticsearch host')
    parser.add_argument('--port', type=int, default=9200, help='Elasticsearch port')
    parser.add_argument('--user', default='elastic', help='Elasticsearch username')
    parser.add_argument('--password', default=os.getenv('ES_PASSWORD'), help='Elasticsearch password (if not provided, check environment variable ES_PASSWORD)')
    parser.add_argument('--api-key', help='Elasticsearch API Key for authentication')
    parser.add_argument('--self-signed', action='store_false', help='Elasticsearch is using self-signed certificates')

    # Elasticsearch indexing arguments
    parser.add_argument('--index', default='ptr-records', help='Elasticsearch index name')
    parser.add_argument('--shards', type=int, default=1, help='Number of shards for the index')
    parser.add_argument('--replicas', type=int, default=1, help='Number of replicas for the index')

    # Performance arguments
    parser.add_argument('--batch-max', type=int, default=10, help='Maximum size in MB of a batch')
    parser.add_argument('--batch-size', type=int, default=5000, help='Number of records to index in a batch')
    parser.add_argument('--batch-threads', type=int, default=2, help='Number of threads to use when indexing in batches')
    parser.add_argument('--retries', type=int, default=10, help='Number of times to retry indexing a batch before failing')
    parser.add_argument('--timeout', type=int, default=30, help='Number of seconds to wait before retrying a batch')

    args = parser.parse_args()

    # Argument validation
    if not os.path.isdir(args.input_path) and not os.path.isfile(args.input_path):
        raise FileNotFoundError(f'Input path {args.input_path} does not exist or is not a file or directory')

    if not args.dry_run:
        if args.batch_size < 1:
            raise ValueError('Batch size must be greater than 0')
        
        elif args.retries < 1:
            raise ValueError('Number of retries must be greater than 0')
        
        elif args.timeout < 5:
            raise ValueError('Timeout must be greater than 4')
        
        elif args.batch_max < 1:
            raise ValueError('Batch max size must be greater than 0')
        
        elif args.batch_threads < 1:
            raise ValueError('Batch threads must be greater than 0')

        elif not args.host:
            raise ValueError('Missing required Elasticsearch argument: host')

        elif not args.api_key and (not args.user or not args.password):
            raise ValueError('Missing required Elasticsearch argument: either user and password or apikey')

        elif args.shards < 1:
            raise ValueError('Number of shards must be greater than 0')

        elif args.replicas < 0:
            raise ValueError('Number of replicas must be greater than 0')

    edx = ElasticIndexer(args.host, args.port, args.user, args.password, args.api_key, args.index, args.dry_run, args.self_signed, args.retries, args.timeout)
    
    if not args.dry_run:
        print(edx.get_cluster_health())

        time.sleep(3) # Delay to allow time for sniffing to complete

        nodes = edx.get_cluster_size()

        logging.info(f'Connected to {nodes:,} Elasticsearch node(s)')

        edx.create_index(args.shards, args.replicas) # Create the index if it does not exist

        chunk = {
            'size': args.batch_size,
            'max_size': args.batch_max * 1024 * 1024, # MB
            'threads': args.batch_threads
        }
        
        chunk['batch'] = nodes * (chunk['size'] * chunk['threads'])
    else:
        chunk = {} # Ugly hack to get this working...

    if os.path.isfile(args.input_path):
        logging.info(f'Processing file: {args.input_path}')
        edx.process_file(args.input_path, args.watch, chunk)

    elif os.path.isdir(args.input_path):
        count = 1
        total = len(os.listdir(args.input_path))
        logging.info(f'Processing {total:,} files in directory: {args.input_path}')
        for file in sorted(os.listdir(args.input_path)):
            file_path = os.path.join(args.input_path, file)
            if os.path.isfile(file_path):
                logging.info(f'[{count:,}/{total:,}] Processing file: {file_path}')
                edx.process_file(file_path, args.watch, chunk)
                count += 1
            else:
                logging.warning(f'[{count:,}/{total:,}] Skipping non-file: {file_path}')



if __name__ == '__main__':
    main()