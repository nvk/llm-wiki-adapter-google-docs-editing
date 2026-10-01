/**
 * LLM Wiki's deliberately small Google Docs control surface.
 *
 * It grants drive.file access to the active document. Planning, approval,
 * suggestion writes, recovery, and verification remain in the local adapter.
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
