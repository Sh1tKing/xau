"""Citations declared by the exact outbound request, never inferred from AI typos."""
import copy
import hashlib
import json
import re
from strategies.entry_context import citation_allowed as entry_context_citation_allowed

VERSION = 'ai1-citations-v1'
MISSING = object()
SHARED_ROOTS = {'m15_bars','m5_bars','h1_bars','patterns','live_quote','atr','h1_trend',
                'm15_trend','m15_ema21','m15_ema21_distance_atr','bar_closure'}
SHARED_RULES = {'max_stop_abs','min_rr','chase_max_atr'}
SHARED_FINALIZATION = {'generation_min_risk','execution_min_stop_clearance','execution_clearance_price',
                       'generation_floor_is_not_execution_floor','stop_rounding','stop_price_version'}
HIDDEN = {'host_verified_region','host_verified_entry_event','host_verified_analysis_checkpoint','host_verified_direction','candidates'}
# These shared roots are intentionally not in SHARED_ROOTS. New child names must
# be reviewed explicitly; an arbitrary subtree can contain another candidate.
DIRECTION_FIELDS = {'version', 'parameters_digest', 'engine_digest', 'source_digest', 'h1_background'}
DIRECTION_LEG_FIELDS = {'state', 'reason', 'event_id', 'epsilon', 'formation_start', 'origin_price',
                        'direction', 'expires_at_utc', 'failure_level', 'm15_invalidated'}
DIRECTION_NESTED = {'boundaries': {'upper', 'lower'},
                    'confirmation': {'bar_time', 'closed_at_utc', 'close', 'atr14'},
                    'failure': {'bar_times', 'level', 'confirmed_at', 'low', 'high'}}
RECENT_COVERAGE = {'source', 'scope', 'status', 'started_utc', 'window_start_utc', 'window_end_utc',
                   'max_records', 'returned_limit', 'cached_records', 'unknown_close_utc_records',
                   'truncated_before_utc', 'eligible_records'}
RECENT_RECORD = {'position_id', 'deal_ids', 'direction', 'profit', 'commission', 'swap', 'fee',
                 'net_profit', 'volume', 'price_open', 'price_close', 'reason_code',
                 'broker_close_time', 'close_utc', 'observed_utc'}
RECENT_ENTRY = {'direction_policy_version', 'route', 'h1_background', 'structure_event_id',
                'protection_kind', 'invalidation_reason', 'strategy_id'}


def _public_shared(parts):
    if parts[0] == 'entry_context':
        return entry_context_citation_allowed(parts)
    if parts[0] == 'direction_context':
        if len(parts) == 2 and parts[1] in DIRECTION_FIELDS:
            return True
        if len(parts) == 3 and parts[1] == 'source_bar_ids' and parts[2] in ('m15', 'm5'):
            return True
        if len(parts) == 3 and parts[1] == 'm15_leg' and parts[2] in DIRECTION_LEG_FIELDS:
            return True
        return (len(parts) == 4 and parts[1] == 'm15_leg'
                and parts[3] in DIRECTION_NESTED.get(parts[2], set()))
    if parts[0] == 'recent_trades':
        if len(parts) == 2 and parts[1] in ('version', 'available'):
            return True
        if len(parts) == 3 and parts[1] == 'coverage' and parts[2] in RECENT_COVERAGE:
            return True
        if len(parts) >= 4 and parts[1] == 'records' and parts[2] in tuple(str(i) for i in range(6)):
            return (len(parts) == 4 and parts[3] in RECENT_RECORD or len(parts) == 5
                    and parts[3] == 'entry_context' and parts[4] in RECENT_ENTRY)
    return False


def pointer_parts(pointer):
    if not isinstance(pointer,str) or not pointer.startswith('/') or re.search(r'~(?![01])',pointer):
        raise ValueError('证据JSON Pointer格式非法')
    return [p.replace('~1','/').replace('~0','~') for p in pointer[1:].split('/')]


def pointer_value(pack,pointer):
    value=pack
    try:
        for token in pointer_parts(pointer):
            if isinstance(value,list):
                if not token.isascii() or not token.isdigit() or len(token)>1 and token.startswith('0'):
                    return MISSING
                value=value[int(token)]
            else:
                value=value[token]
        return value
    except (KeyError,IndexError,TypeError,ValueError):
        return MISSING


def scope_allowed(pointer,index,action):
    try:
        parts=pointer_parts(pointer)
    except ValueError:
        return False
    if any(p in HIDDEN for p in parts):
        return False
    if parts[0] in ('direction_context', 'recent_trades', 'entry_context'):
        return _public_shared(parts)
    if len(parts)>=3 and parts[:2]==['opportunities',str(index)]:
        if 'strategy_evidence' in parts:
            at=parts.index('strategy_evidence')
            return len(parts)>at+1 and parts[at+1]==action
        return True
    if parts[0] in SHARED_ROOTS or parts[:2]==['computed_evidence','price_action']:
        return True
    if parts[0]=='rules':
        return (len(parts)>=2 and parts[1] in SHARED_RULES or len(parts)>=3
                and parts[1]=='price_finalization' and parts[2] in SHARED_FINALIZATION)
    return False


def _escape(key):
    return str(key).replace('~','~0').replace('/','~1')


def _paths(value,base,depth):
    if value is None:
        return []
    result=[base]
    if depth>0 and isinstance(value,dict):
        for key,child in sorted(value.items()):
            if key not in HIDDEN:
                result.extend(_paths(child,base+'/'+_escape(key),depth-1))
    elif depth>0 and isinstance(value,list):
        for index,child in enumerate(value):
            result.extend(_paths(child,base+'/'+str(index),depth-1))
    return result


def build_catalog(pack):
    # Format guidance contains current catalog IDs, not source facts; including
    # it would create a circular scope hash. It is never an allowed citation root.
    source={k:v for k,v in pack.items() if k not in ('evidence_catalog','quality_rubric','quality_reference_examples')}
    scope=hashlib.sha256(json.dumps(source,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()[:10]
    groups=[]
    def add(base,paths,owner=None):
        paths=list(dict.fromkeys(paths))
        paths=[p for p in paths if pointer_value(pack,p) is not MISSING and pointer_value(pack,p) is not None]
        if 'opportunities' in pack:
            paths=[p for p in paths if any(scope_allowed(p,i,c.get('action')) for i,c in enumerate(pack['opportunities']) if c)]
        if not paths: return
        group={'base':base,'refs':{scope+'.'+str(len(groups))+'.'+str(n):p[len(base):] for n,p in enumerate(paths)}}
        if owner: group.update(opportunity_id=owner['opportunity_id'],direction=owner['action'])
        groups.append(group)
    simple=[]
    for key in sorted(SHARED_ROOTS):
        if key not in pack: continue
        value=pack[key]
        if key.endswith('_bars'):
            # Older bars remain in the full input and can cite the array; no giant leaf directory.
            paths=['/'+key]+['/'+key+'/'+str(i) for i in range(max(0,len(value)-8),len(value))]
            add('/'+key,paths)
        elif isinstance(value,(dict,list)):
            add('/'+key,_paths(value,'/'+key,1))
        else: simple.append('/'+key)
    add('',simple)
    for root in ('direction_context', 'recent_trades', 'entry_context'):
        value = pack.get(root)
        if isinstance(value, dict):
            add('/'+root, [path for path in _paths(value, '/'+root, 4)
                           if _public_shared(pointer_parts(path))])
    rules=pack.get('rules',{})
    add('/rules',['/rules/'+k for k in sorted(SHARED_RULES) if k in rules]+[
        '/rules/price_finalization/'+k for k in sorted(SHARED_FINALIZATION) if k in rules.get('price_finalization',{})])
    pa=pack.get('computed_evidence',{}).get('price_action',{})
    for tf,actions in sorted(pa.items()):
        for action,value in sorted(actions.items()):
            base='/computed_evidence/price_action/'+_escape(tf)+'/'+_escape(action)
            add(base,[base]+[base+'/'+key for key in ('score','bar_time','checks') if key in value])
    if 'opportunities' in pack:
        for i,candidate in enumerate(pack['opportunities']):
            if not candidate: continue
            base='/opportunities/'+str(i)
            for name in ('strategy_contract','reference_price','coarse_reference_price','local_plan',
                         'local_plan_status','local_plan_feasibility','structural_blockers','entry_event'):
                if name in candidate:
                    value=candidate[name]; prefix=base+'/'+name
                    paths=[prefix]+([prefix+'/'+_escape(k) for k,v in sorted(value.items())
                                     if isinstance(v,(int,float,bool))] if isinstance(value,dict) else [])
                    add(prefix,paths,candidate)
            side=candidate['action']
            assessment=candidate.get('strategy_evidence',{}).get(side,{})
            prefix=base+'/strategy_evidence/'+side
            add(prefix,[prefix]+[prefix+'/'+_escape(k) for k,v in sorted(assessment.items())
                                 if k!='evidence' and isinstance(v,(int,float,bool))],candidate)
            facts=assessment.get('evidence',{})
            # Parent facts cover arbitrary custom definitions, and children expose common fields.
            prefix+='/evidence'
            paths=[prefix]+[prefix+'/'+_escape(k) for k in sorted(facts) if k not in HIDDEN]
            for key,value in sorted(facts.items()):
                if key not in HIDDEN and isinstance(value,dict):
                    paths.extend(prefix+'/'+_escape(key)+'/'+_escape(k) for k,v in sorted(value.items())
                                 if isinstance(v,(int,float,bool)))
            add(prefix,paths,candidate)
    else:
        for key in ('strategy_evidence','computed_evidence','local_plan_feasibility','strategy_contract','reference_price'):
            if key in pack: add('/'+key,_paths(pack[key],'/'+key,2))
    return {'version':VERSION,'scope':scope,'groups':groups}


def catalog_bindings(pack):
    catalog=pack.get('evidence_catalog')
    if catalog is None: return {}
    if catalog!=build_catalog(pack):
        raise ValueError('AI-1证据目录与本次请求快照不一致')
    return {identifier:group['base']+suffix for group in catalog['groups'] for identifier,suffix in group['refs'].items()}


def resolve_references(payload,pack):
    """Explicit advertised IDs only. Real legacy pointers retain their original meaning."""
    result=copy.deepcopy(payload)
    references=catalog_bindings(pack)
    def visit(value):
        if isinstance(value,dict):
            for key,child in value.items():
                if key=='evidence_refs' and isinstance(child,list):
                    decoded=[]
                    for ref in child:
                        if isinstance(ref,str) and ref.startswith('/'):
                            decoded.append(ref)  # Never repair nesting, aliases or list indices.
                        elif isinstance(ref,str) and ref in references:
                            decoded.append(references[ref])
                        else:
                            raise ValueError('AI-1证据ID无效或不属于本次快照: '+str(ref))
                    value[key]=decoded
                else: visit(child)
        elif isinstance(value,list):
            for child in value: visit(child)
    visit(result)
    return result
