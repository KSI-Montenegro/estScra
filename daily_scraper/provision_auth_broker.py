#!/usr/bin/env python3
"""Deploy the private auth broker; write local-only TextBee setup credentials."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid


class Cloudflare:
    def __init__(self, token): self.token = token
    def call(self, method, path, data=None, mime='application/json', missing=False):
        body = json.dumps(data).encode() if data is not None and not isinstance(data, bytes) else data
        req = Request('https://api.cloudflare.com/client/v4'+path, method=method, data=body,
                      headers={'Authorization':'Bearer '+self.token, 'Content-Type':mime,
                               'User-Agent':'GRADEN-Auth-Provision/1.0'})
        try:
            with urlopen(req, timeout=60) as response: result=json.load(response)
        except HTTPError as exc:
            if missing and exc.code==404:return None
            # Do not print request/response bodies: secret API calls include values.
            raise RuntimeError('Cloudflare HTTP '+str(exc.code)) from None
        if not result.get('success'):raise RuntimeError('Cloudflare rejected auth-broker operation')
        return result.get('result')


def private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream,indent=2);stream.write('\n')
    path.chmod(0o600)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--token-file',type=Path,required=True)
    p.add_argument('--account',required=True)
    p.add_argument('--device-id',required=True)
    p.add_argument('--worker',default='graden-patuljak-auth')
    p.add_argument('--credentials-file',type=Path,required=True)
    args=p.parse_args()
    if not re.fullmatch(r'[a-f0-9]{32}',args.account) or not re.fullmatch(r'[a-f0-9]{24}',args.device_id) or not re.fullmatch(r'[a-z0-9-]{1,63}',args.worker):p.error('Invalid account, device or worker identifier')
    cf=Cloudflare(args.token_file.read_text().strip());base='/accounts/'+args.account+'/workers'
    existing=cf.call('GET',base+'/scripts/'+args.worker+'/settings',missing=True)
    if existing and not any(b.get('name')=='PROJECT' and b.get('text')=='graden-patuljak-auth-v1' for b in existing['bindings']):raise RuntimeError('Refusing to overwrite an unrelated Worker')
    old=json.loads(args.credentials_file.read_text()) if args.credentials_file.exists() else {}
    if old and (old.get('account')!=args.account or old.get('worker')!=args.worker):raise RuntimeError('Credential file belongs to another deployment')
    if existing and not old:raise RuntimeError('Existing broker requires its original local credential file; refusing implicit key rotation')
    config={'account':args.account,'worker':args.worker,'device_id':args.device_id,
            'client_key':old.get('client_key') or secrets.token_urlsafe(48),
            'signing_secret':old.get('signing_secret') or secrets.token_hex(32)}
    domain=cf.call('GET',base+'/subdomain')['subdomain']
    config['api_base']='https://'+args.worker+'.'+domain+'.workers.dev'
    # Save first, so a deployment interrupted after creation can safely resume.
    private_json(args.credentials_file,config)
    metadata={'main_module':'worker.mjs','compatibility_date':'2026-01-01',
              'bindings':[{'type':'plain_text','name':'PROJECT','text':'graden-patuljak-auth-v1'},
                          {'type':'plain_text','name':'TEXTBEE_DEVICE_ID','text':args.device_id},
                          {'type':'durable_object_namespace','name':'AUTH_STATE','class_name':'PatuljakAuth'}]}
    if not existing:metadata['migrations']={'new_tag':'v1','new_sqlite_classes':['PatuljakAuth']}
    code=(Path(__file__).parent/'auth_broker/worker.mjs').read_bytes();boundary='graden-'+uuid.uuid4().hex;parts=[]
    for name,mime,data in [('metadata','application/json',json.dumps(metadata).encode()),('worker.mjs','application/javascript+module',code)]:
        parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+name+'"'+('; filename="worker.mjs"' if name.endswith('.mjs') else '')+'\r\nContent-Type: '+mime+'\r\n\r\n').encode()+data+b'\r\n')
    cf.call('PUT',base+'/scripts/'+args.worker,b''.join(parts)+('--'+boundary+'--\r\n').encode(),mime='multipart/form-data; boundary='+boundary)
    for name,value in [('AUTH_CLIENT_KEY',config['client_key']),('TEXTBEE_WEBHOOK_SECRET',config['signing_secret'])]:
        cf.call('PUT',base+'/scripts/'+args.worker+'/secrets',{'name':name,'text':value,'type':'secret_text'})
    cf.call('POST',base+'/scripts/'+args.worker+'/subdomain',{'enabled':True,'previews_enabled':False})
    setup={'name':'Patuljak login codes','deliveryUrl':config['api_base']+'/webhooks/textbee',
           'signingSecret':config['signing_secret'],'events':['MESSAGE_RECEIVED']}
    setup_path=args.credentials_file.with_name('textbee-webhook-setup.json');private_json(setup_path,setup)
    print(json.dumps({'api_base':config['api_base'],'webhook_url':setup['deliveryUrl'],
                      'private_textbee_setup_file':str(setup_path),'device_id':args.device_id}))


if __name__=='__main__':main()
