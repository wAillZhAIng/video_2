"""Compare immutable model inputs independently of recorded approval state."""
from copy import deepcopy


def _content(value):
    result=deepcopy(value)
    result.pop('fingerprint',None)
    snapshot=result.get('screenplay_snapshot')
    if not isinstance(snapshot,dict):return result
    shot=snapshot.get('shot')
    if isinstance(shot,dict):
        for key in ('updated_at','status','approved_at','rejection_reason'):
            shot.pop(key,None)
    detail=snapshot.get('detail')
    if isinstance(detail,dict):
        detail.pop('updated_at',None)
        source=detail.get('source_json')
        if isinstance(source,dict):source.pop('studio_acceptance',None)
    cinematic=snapshot.get('cinematic')
    preflight=cinematic.get('preflight') if isinstance(cinematic,dict) else None
    if isinstance(preflight,dict):
        for key in ('ready','findings','counts'):
            preflight.pop(key,None)
    return result


def context_reuse_reason(previous,current):
    """Keep old fingerprints readable without rewriting frozen snapshots.

    Only the known approval/diagnostic fields may differ. The independently
    recorded generation-input hash and every remaining model input must match.
    Current preflight and output validation still run in the dispatch caller.
    """
    if not isinstance(previous,dict) or not isinstance(current,dict):return None
    fingerprint=previous.get('fingerprint')
    if isinstance(fingerprint,str) and fingerprint and fingerprint==current.get('fingerprint'):
        return 'identical_fingerprint'
    identity=previous.get('generation_inputs_hash')
    if not isinstance(identity,str) or not identity or identity!=current.get('generation_inputs_hash'):
        return None
    for value in (previous,current):
        snapshot=value.get('screenplay_snapshot') or {}
        shot=snapshot.get('shot') or {}
        if shot.get('status') not in {None,'draft','approved'} or shot.get('rejection_reason'):
            return None
    current_preflight=((current.get('screenplay_snapshot') or {}).get('cinematic') or {}).get('preflight') or {}
    if any(item.get('severity')=='error' for item in current_preflight.get('findings',[]) if isinstance(item,dict)):
        return None
    if _content(previous)==_content(current):
        return 'identical_content_approval_metadata_only'
    return None
