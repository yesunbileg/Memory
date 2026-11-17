// Bound the actual serialized UTF-8 body, without truncating visible text.
export const MAX_BODY_BYTES = 16 * 1024 * 1024;
const MAX_CONVERSATIONS = 100;
const encoder = new TextEncoder();
const oversized = () =>
  Object.assign(new Error('A conversation item exceeds the local receiver byte limit.'), {
    code: 'body_too_large',
  });

export function requestGroups(source, adapter, account, conversations, archiveId = null) {
  const serialize = (items) =>
    JSON.stringify({
      source,
      adapter,
      account,
      ...(archiveId ? { archive_instance_id: archiveId } : {}),
      conversations: items,
    });
  const fits = (body) => encoder.encode(body).length <= MAX_BODY_BYTES;
  const groups = [];
  let batch = [];
  const flush = () => {
    if (batch.length) groups.push({ bodies: [serialize(batch)], count: batch.length });
    batch = [];
  };
  for (const conversation of conversations) {
    if (fits(serialize([conversation]))) {
      if (batch.length >= MAX_CONVERSATIONS || !fits(serialize([...batch, conversation]))) flush();
      batch.push(conversation);
      continue;
    }
    flush();
    const messages = conversation.messages;
    // Derived occurrence IDs depend on the whole snapshot. Chunk only at native
    // message identities so replay never changes normalization identities.
    if (!Array.isArray(messages) || !messages.length) throw oversized();
    const nativeIds = messages.map((message) =>
      [message?.clientId, message?.metadata?.id, message?.id].find(
        (id) => typeof id === 'string' && id,
      ),
    );
    const identities = new Set(nativeIds);
    if (nativeIds.some((id) => !id) || identities.size !== nativeIds.length) throw oversized();
    // Normalization can append group suffixes for multipart messages. Refuse a
    // potentially colliding native group ID rather than validating each chunk
    // in isolation and overwriting an earlier message in the next request.
    if (
      nativeIds.some((id) => {
        const group = /^(.*):(text|progress|thinking_summary):[0-9]+$/.exec(id);
        return group && identities.has(group[1]);
      })
    )
      throw oversized();
    const bodies = [];
    for (let start = 0; start < messages.length; ) {
      let low = start + 1,
        high = messages.length,
        end = start;
      while (low <= high) {
        const middle = Math.floor((low + high) / 2);
        if (fits(serialize([{ ...conversation, messages: messages.slice(start, middle) }]))) {
          end = middle;
          low = middle + 1;
        } else high = middle - 1;
      }
      if (end === start) throw oversized();
      bodies.push(serialize([{ ...conversation, messages: messages.slice(start, end) }]));
      start = end;
    }
    groups.push({ bodies, count: 1 });
  }
  flush();
  return groups;
}
