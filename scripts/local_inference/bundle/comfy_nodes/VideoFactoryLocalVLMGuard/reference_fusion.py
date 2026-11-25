"""Fuse verified views before native planning; never infer identity from order."""
from dataclasses import asdict, replace
import re


def order_written_sections(prompt):
    """Reorder six unique model-written blocks without altering their bodies."""
    names=('subject_definitions','summary','retention_analysis','detailed_description',
           'overall_soundscape','non_diegetic_music')
    matches=list(re.finditer(r'(?m)^([a-z_]+):[ \t]*(?:\r?\n)',prompt))
    if (len(matches)!=6 or set(m[1] for m in matches)!=set(names)
            or prompt[:matches[0].start()].strip()):return prompt,None
    blocks={match[1]:prompt[match.start():matches[i+1].start() if i+1<len(matches) else len(prompt)]
            for i,match in enumerate(matches)}
    if tuple(match[1] for match in matches)==names:return prompt,None
    # A section name quoted as dialogue/content is not a structural heading.
    for match in matches:
        before=prompt[:match.start()]
        if before.count('<d>')!=before.count('</d>'):return prompt,None
    ordered=''.join(blocks[name].rstrip('\r\n')+'\n\n' for name in names).rstrip()+'\n'
    return ordered,{'original_order':[m[1] for m in matches],'canonical_order':list(names),
                    'all_section_bodies_preserved':True,'content_added_or_removed':False}


def merge_retention_rows(prompt):
    """Combine duplicate fully-preserved rows without losing their facts.

    One subject may have several observations (appearance, holder state, etc.)
    but the native schema requires one retention entry. Conflicting markers,
    ambiguous cut references and unstructured rows remain untouched for the
    real model repair and validators to reject or correct.
    """
    section=re.search(r'^retention_analysis[ \t]*:(.*?)(?=^(?:detailed_description|overall_soundscape|non_diegetic_music)[ \t]*:|\Z)',prompt,re.M|re.S)
    if not section:return prompt,[]
    lines=section[1].splitlines(keepends=True)
    entries={}
    heads={}
    pattern=re.compile(r'^[ \t]*(<Subject \d+>)[ \t]*(?:\(([^)\r\n]*)\))?[ \t]*:[ \t]*(fully_preserved)[ \t]*-[ \t]*([^\r\n]+)[\r\n]*$')
    for index,line in enumerate(lines):
        head=re.match(r'^[ \t]*(<Subject \d+>)[ \t]*(?:\([^\r\n]*\))?[ \t]*:',line)
        if head:heads.setdefault(head[1],[]).append(index)
        match=pattern.fullmatch(line)
        if match:entries.setdefault(match[1],[]).append((index,match))
    replacements={};removed=set();edits=[]
    for label,rows in entries.items():
        if len(rows)<2 or len(rows)!=len(heads[label]):continue
        canonical=[(index,match) for index,match in rows if match[2]
                   and re.fullmatch(r'appears in \[Shot \d+\](?:,\s*\[Shot \d+\])*',match[2])]
        if not canonical:continue
        if any(match[2] and 'Shot' in match[2] and (index,match) not in canonical for index,match in rows):continue
        shots=list(dict.fromkeys(tag for _,match in canonical for tag in re.findall(r'\[Shot \d+\]',match[2])))
        bodies=[]
        for index,match in rows:
            prefix=(match[2]+': ') if match[2] and (index,match) not in canonical else ''
            bodies.append(prefix+match[4])
        first=rows[0][0]
        replacements[first]=label+' (appears in '+', '.join(shots)+'): fully_preserved - '+' '.join(bodies)+'\n'
        removed.update(index for index,_ in rows[1:])
        edits.append({'subject':label,'original_rows':[lines[index].rstrip('\r\n') for index,_ in rows],
                      'all_original_bodies_preserved':True,'source_images_removed':False,
                      'visual_facts_added':False,'marker':'fully_preserved'})
    if not edits:return prompt,[]
    merged=''.join(replacements.get(index,line) for index,line in enumerate(lines) if index not in removed)
    return prompt[:section.start(1)]+merged+prompt[section.end(1):],edits


def fuse_product_subjects(subjects, contract, proof):
    if not contract or not proof or proof.get('status') != 'PASS':
        return subjects, {'status': 'NOT_APPLIED', 'reason': 'no complete visual product identity proof'}
    approved = proof['approved_product_descriptors_by_native']
    replacements, removed, groups = {}, set(), []
    product_sources=set()
    for group in contract.get('groups', []):
        if group.get('category') != 'product':
            continue
        sources = group.get('appearance_native') or []
        product_sources.update(sources)
        matches = []
        for source in sources:
            candidates = [subject for subject in subjects
                          if source in subject.sources and subject.descriptor in approved.get(source, [])]
            if len(candidates) != 1:
                raise ValueError(f'Global fusion cannot uniquely bind {source} to the verified product {group["group"]}')
            if candidates[0] not in matches:
                matches.append(candidates[0])
        if not matches:
            continue
        if any(subject.label in removed or subject.label in replacements for subject in matches):
            raise ValueError('Global fusion found conflicting explicit product identities')
        anchor = matches[0]
        fused = replace(anchor,
            sources=list(dict.fromkeys(source for subject in matches for source in subject.sources)),
            attributes=list(dict.fromkeys(value for subject in matches for value in subject.attributes)))
        replacements[anchor.label] = fused
        removed.update(subject.label for subject in matches[1:])
        groups.append({'group_id': group['group'], 'category': 'product',
            'source': 'explicit product grouping plus actual cross-image identity verification',
            'native_sources': list(fused.sources), 'original_subjects': [asdict(subject) for subject in matches],
            'fused_subject': asdict(fused), 'attributes_discarded': False,
            'per_view_pose_source': 'original analysis cards retained independently'})
    # Product photographs authorize the verified product identity. Their
    # holder, backdrop and decorations remain observed context, not extra
    # performers or merchandise that the new scene must reproduce.
    target_labels=set(replacements)|removed
    auxiliary=[subject for subject in subjects if subject.label not in target_labels
               and subject.sources and set(subject.sources)<=product_sources]
    auxiliary_labels={subject.label for subject in auxiliary}
    result = [replacements.get(subject.label, subject) for subject in subjects
              if subject.label not in removed|auxiliary_labels]
    result = [replace(subject, label=f'<Subject {index}>') for index, subject in enumerate(result, 1)]
    return result, {'status': 'PASS', 'groups': groups, 'original_subject_count': len(subjects),
        'fused_subject_count': len(result), 'image_inputs_removed': False,
        'auxiliary_context_observations':[asdict(subject) for subject in auxiliary],
        'auxiliary_scope':'all original image cards and pixels retained; non-target product-photo context is not asserted as a new scene identity',
        'unbound_relationships': 'retained as independent subjects; never merged by filename, folder or array position',
        'subjects': [asdict(subject) for subject in result]}


def bind_existing_mentions(prompt, targets):
    """Add only missing identity tokens to unique, already written noun phrases.

    Ambiguous descriptors and quoted/dialogue content are untouched. The caller
    supplies only targets proven by image identity and written source bindings;
    every native schema and semantic gate still runs on the resulting text.
    """
    section=re.search(r'(?ms)(^detailed_description:[ \t]*\n)(.*?)(?=^(?:overall_soundscape|non_diegetic_music):|\Z)',prompt)
    if not section:return prompt,[]
    detail=section[2]
    shot=re.search(r'(?m)^\s*\[Shot\s+\d+\]',detail)
    if not shot:return prompt,[]
    heads=re.findall(r'(?m)^\s*<Subject\s+\d+>\s+is\s+(.+?)\s+in\s+<Picture\s+\d+>',prompt[:section.start()])
    edits=[]
    for target in targets:
        label,descriptor=target['subject'],target['descriptor']
        if label in detail or not descriptor.strip():continue
        head=descriptor.casefold().split()[-1]
        if sum(value.casefold().split()[-1]==head for value in heads)!=1:continue
        pattern=re.compile(r'(?<!\w)'+re.escape(descriptor)+r'(?!\w)',re.I)
        protected=list(re.finditer(r'<d>.*?</d>|"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’',detail,re.S))
        match=next((candidate for candidate in pattern.finditer(detail,shot.start())
                    if not any(span.start()<=candidate.start()<span.end() for span in protected)),None)
        if not match:continue
        original=match[0]
        detail=detail[:match.start()]+label+' ('+original+')'+detail[match.end():]
        edits.append({'subject':label,'original_noun_phrase':original,'source_references':target['sources'],
                      'visual_facts_added':False,'source_text_removed':False})
    return prompt[:section.start(2)]+detail+prompt[section.end(2):],edits
