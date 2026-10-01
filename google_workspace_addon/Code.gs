/**
 * LLM Wiki's deliberately small Google Docs control surface.
 *
 * It grants drive.file access to the active document and provides a narrow
 * Google-only API-executable bridge. Planning, approval, revision locking,
 * suggestion writes, recovery, and verification remain governed locally.
 */

function onDocsHomepage(e) {
  return buildDocumentCard_(e);
}

function onFileScopeGranted(e) {
  return buildDocumentCard_(e);
}

function requestCurrentDocumentAccess() {
  return CardService.newEditorFileScopeActionResponseBuilder()
      .requestFileScopeForActiveDocument()
      .build();
}

function buildDocumentCard_(e) {
  const docs = e && e.docs ? e.docs : {};
  const allowed = docs.addonHasFileScopePermission === true;
  const builder = CardService.newCardBuilder()
      .setHeader(CardService.newCardHeader().setTitle('LLM Wiki'));
  const section = CardService.newCardSection();

  if (!allowed) {
    section.addWidget(
        CardService.newTextParagraph().setText(
            'Share this document with LLM Wiki. Other Drive files stay private.'));
    const action = CardService.newAction()
        .setFunctionName('requestCurrentDocumentAccess');
    section.addWidget(
        CardService.newTextButton()
            .setText('Share this document')
            .setTextButtonStyle(CardService.TextButtonStyle.FILLED)
            .setOnClickAction(action));
  } else {
    const title = escapeHtml_(typeof docs.title === 'string' ? docs.title : 'Document');
    section.setHeader(title);
    section.addWidget(
        CardService.newTextParagraph().setText(
            '<b>Ready</b><br>Ask LLM Wiki to edit this document. ' +
            'Every change arrives as a suggestion for review.'));
  }

  return builder.addSection(section).build();
}

function escapeHtml_(value) {
  return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
}

/**
 * Returns one file-scoped document through the Apps Script execution API.
 * The caller never receives this script's OAuth token.
 */
function llmWikiBridgeGetDocument(documentId) {
  const id = validateDocumentId_(documentId);
  return docsApiRequest_(
      'get',
      '/documents/' + encodeURIComponent(id) +
          '?includeTabsContent=true' +
          '&suggestionsViewMode=SUGGESTIONS_INLINE' +
          '&commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED');
}

/**
 * Applies only the adapter's bounded native-suggestion request shape.
 */
function llmWikiBridgeBatchUpdate(documentId, body) {
  const id = validateDocumentId_(documentId);
  validateSuggestionBatch_(body);
  return docsApiRequest_(
      'post', '/documents/' + encodeURIComponent(id) + ':batchUpdate', body);
}

function docsApiRequest_(method, path, body) {
  const options = {
    method: method,
    headers: {Authorization: 'Bearer ' + ScriptApp.getOAuthToken()},
    muteHttpExceptions: true,
  };
  if (body !== undefined) {
    options.contentType = 'application/json; charset=utf-8';
    options.payload = JSON.stringify(body);
  }
  const response = UrlFetchApp.fetch('https://docs.googleapis.com/v1' + path, options);
  const status = response.getResponseCode();
  if (status < 200 || status >= 300) {
    throw new Error('Google Docs API request failed with HTTP ' + status);
  }
  let value;
  try {
    value = JSON.parse(response.getContentText());
  } catch (error) {
    throw new Error('Google Docs API returned invalid JSON');
  }
  if (!isPlainObject_(value)) {
    throw new Error('Google Docs API returned a non-object response');
  }
  return value;
}

function validateDocumentId_(value) {
  if (typeof value !== 'string' ||
      !/^[A-Za-z0-9_-]{10,256}$/.test(value)) {
    throw new Error('Invalid Google document ID');
  }
  return value;
}

function validateSuggestionBatch_(body) {
  requireKeys_(body, ['commentUpdateState', 'requests', 'writeControl'], 'batch');
  if (body.commentUpdateState !== 'ALL_SAVED') {
    throw new Error('Batch must require ALL_SAVED comments');
  }
  requireKeys_(body.writeControl, ['requiredRevisionId', 'writeMode'], 'write control');
  if (body.writeControl.writeMode !== 'SUGGEST' ||
      typeof body.writeControl.requiredRevisionId !== 'string' ||
      body.writeControl.requiredRevisionId.length < 1 ||
      body.writeControl.requiredRevisionId.length > 2048) {
    throw new Error('Batch must use SUGGEST with a required revision');
  }
  if (!Array.isArray(body.requests) || body.requests.length < 1 ||
      body.requests.length > 40) {
    throw new Error('Batch must contain 1-40 requests');
  }
  body.requests.forEach(validateSuggestionRequest_);
}

function validateSuggestionRequest_(request) {
  if (!isPlainObject_(request)) {
    throw new Error('Invalid suggestion request');
  }
  const keys = Object.keys(request);
  if (keys.length !== 1 ||
      (keys[0] !== 'deleteContentRange' && keys[0] !== 'insertText')) {
    throw new Error('Unsupported suggestion request');
  }
  if (keys[0] === 'deleteContentRange') {
    requireKeys_(request.deleteContentRange, ['range'], 'delete request');
    validateRange_(request.deleteContentRange.range);
    return;
  }
  const insertion = request.insertText;
  if (!isPlainObject_(insertion) || typeof insertion.text !== 'string' ||
      insertion.text.length > 100000) {
    throw new Error('Invalid insert request');
  }
  const insertionKeys = Object.keys(insertion).sort();
  if (sameKeys_(insertionKeys, ['location', 'text'])) {
    validateLocation_(insertion.location);
    return;
  }
  if (sameKeys_(insertionKeys, ['endOfSegmentLocation', 'text'])) {
    validateEndLocation_(insertion.endOfSegmentLocation);
    return;
  }
  throw new Error('Insert request must have one bounded location');
}

function validateRange_(range) {
  if (!isPlainObject_(range)) {
    throw new Error('Invalid delete range');
  }
  const keys = Object.keys(range).sort();
  if (!sameKeys_(keys, ['endIndex', 'startIndex']) &&
      !sameKeys_(keys, ['endIndex', 'startIndex', 'tabId'])) {
    throw new Error('Invalid delete range fields');
  }
  if (!isIndex_(range.startIndex) || !isIndex_(range.endIndex) ||
      range.endIndex <= range.startIndex) {
    throw new Error('Invalid delete range indexes');
  }
  validateOptionalTabId_(range);
}

function validateLocation_(location) {
  if (!isPlainObject_(location)) {
    throw new Error('Invalid insert location');
  }
  const keys = Object.keys(location).sort();
  if (!sameKeys_(keys, ['index']) && !sameKeys_(keys, ['index', 'tabId'])) {
    throw new Error('Invalid insert location fields');
  }
  if (!isIndex_(location.index)) {
    throw new Error('Invalid insert index');
  }
  validateOptionalTabId_(location);
}

function validateEndLocation_(location) {
  if (!isPlainObject_(location)) {
    throw new Error('Invalid end location');
  }
  const keys = Object.keys(location);
  if (keys.length > 1 || (keys.length === 1 && keys[0] !== 'tabId')) {
    throw new Error('Invalid end location fields');
  }
  validateOptionalTabId_(location);
}

function validateOptionalTabId_(value) {
  if (Object.prototype.hasOwnProperty.call(value, 'tabId') &&
      (typeof value.tabId !== 'string' || value.tabId.length < 1 ||
       value.tabId.length > 256)) {
    throw new Error('Invalid tab ID');
  }
}

function requireKeys_(value, expected, label) {
  if (!isPlainObject_(value) || !sameKeys_(Object.keys(value).sort(), expected.slice().sort())) {
    throw new Error('Invalid ' + label + ' fields');
  }
}

function sameKeys_(actual, expected) {
  return actual.length === expected.length &&
      actual.every(function(value, index) { return value === expected[index]; });
}

function isPlainObject_(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isIndex_(value) {
  return Number.isInteger(value) && value >= 0 && value <= 1000000000;
}
