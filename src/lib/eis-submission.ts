const toTrimmedString = (value: unknown): string => String(value ?? '').trim();

const cleanUserMessage = (value: unknown): string => {
  const raw = toTrimmedString(value);
  if (!raw) return '';

  const htmlStart = raw.search(/<!doctype|<html|<head|<body/i);
  const withoutHtml = htmlStart >= 0 ? raw.slice(0, htmlStart) : raw;
  const message = withoutHtml
    .replace(/<[^>]*>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[;,:-]\s*$/, '');

  if (
    /HTTP Error 500\.30|CloudGuard|Gateway Timeout|MRA request failed.*\b(500|502|503|504)\b/i.test(raw)
  ) {
    return 'MRA EIS is temporarily unavailable.';
  }

  if (!message) return 'MRA EIS returned an error while processing the sale.';
  return message.length > 320 ? `${message.slice(0, 317).trim()}...` : message;
};

export const resolveEisSubmissionFailureReason = (order: unknown): string => {
  const source = (order && typeof order === 'object' ? order : {}) as any;
  const metadata = source.eisValidationMetadata || source.eis_validation_metadata || {};
  const submission =
    source.mraSubmission ||
    source.mra_submission ||
    metadata.mraSubmission ||
    metadata.mra_submission ||
    {};
  const status = toTrimmedString(source.eisStatus ?? source.eis_status).toUpperCase();
  const syncState = toTrimmedString(source.eisSyncState ?? source.eis_sync_state).toUpperCase();
  const syncStatus = toTrimmedString(source.syncStatus ?? source.sync_status).toLowerCase();
  const state = toTrimmedString(
    submission.state ??
      submission.submission_state ??
      source.submissionState ??
      source.submission_state ??
      source.fiscalReceiptState ??
      source.fiscal_receipt_state
  ).toLowerCase();
  const submissionStatus = toTrimmedString(
    submission.status ?? source.eisSubmissionStatus ?? source.eis_submission_status ?? source.fiscal_receipt_status
  ).toLowerCase();
  const submissionEisStatus = toTrimmedString(
    submission.eis_status ?? submission.eisStatus
  ).toUpperCase();
  const retryBlocked = source.syncRetryBlocked === true || source.sync_retry_blocked === true;
  const reason = toTrimmedString(source.reason).toLowerCase();
  const isFailure =
    status === 'REJECTED' ||
    status === 'FAILED' ||
    syncState === 'FAILED' ||
    ['failed', 'error', 'rejected'].includes(syncStatus) ||
    ['rejected', 'failed', 'failed_before_submission', 'invalid_response', 'error'].includes(state) ||
    ['rejected', 'failed', 'error'].includes(submissionStatus) ||
    submissionEisStatus === 'REJECTED' ||
    submissionEisStatus === 'FAILED' ||
    retryBlocked ||
    reason === 'eis_rejected';

  if (!isFailure) return '';

  const candidates = [
    submission.message,
    submission.error,
    submission.details,
    Array.isArray(submission.errors)
      ? submission.errors.map((error: unknown) => cleanUserMessage(error)).filter(Boolean).join('; ')
      : '',
    source.syncError,
    source.sync_error,
    source.error,
    source.submissionMessage,
    source.submission_message,
    source.fiscalReceiptMessage,
    source.fiscal_receipt_message,
    metadata.message,
    metadata.error,
    metadata.details,
    source.reason,
  ];
  for (const candidate of candidates) {
    const message = cleanUserMessage(candidate);
    if (message) return message;
  }

  return status === 'REJECTED' || state === 'rejected' || reason === 'eis_rejected'
    ? 'MRA rejected the fiscal sale.'
    : 'Fiscal receipt submission failed.';
};

export const isEisSaleNotCreated = (source: unknown): boolean => {
  const value = (source && typeof source === 'object' ? source : {}) as any;
  const metadata = value.eisValidationMetadata || value.eis_validation_metadata || {};
  const submission =
    value.mraSubmission ||
    value.mra_submission ||
    metadata.mraSubmission ||
    metadata.mra_submission ||
    {};
  const state = toTrimmedString(
    submission.state ??
      submission.submission_state ??
      value.submissionState ??
      value.submission_state
  ).toLowerCase();
  const reason = toTrimmedString(value.reason).toLowerCase();
  const status = toTrimmedString(value.eisStatus ?? value.eis_status).toUpperCase();
  const submissionStatus = toTrimmedString(
    submission.status ?? value.eisSubmissionStatus ?? value.eis_submission_status
  ).toLowerCase();
  const submissionEisStatus = toTrimmedString(
    submission.eis_status ?? submission.eisStatus
  ).toUpperCase();
  const retryable = submission.retryable === true || value.retryable === true;

  const explicitlyRejected =
    reason === 'eis_rejected' ||
    status === 'REJECTED' ||
    submissionStatus === 'rejected' ||
    submissionEisStatus === 'REJECTED' ||
    state === 'rejected';

  if (explicitlyRejected || reason === 'eis_submission_blocked') {
    return true;
  }

  if (retryable) {
    return false;
  }

  return ['failed_before_submission', 'failed_before_order_create'].includes(state);
};
export type EisSaleDisplayStatus = 'Fiscal Failed' | 'EIS Pending' | 'EIS Submitted';

export const resolveEisSaleStatusLabel = (
  order: unknown,
  eisEnabled = false
): EisSaleDisplayStatus | '' => {
  const source = (order && typeof order === 'object' ? order : {}) as any;
  const metadata = source.eisValidationMetadata || source.eis_validation_metadata || {};
  const submission =
    source.mraSubmission ||
    source.mra_submission ||
    metadata.mraSubmission ||
    metadata.mra_submission ||
    {};
  const status = toTrimmedString(source.eisStatus ?? source.eis_status).toUpperCase();
  const syncState = toTrimmedString(source.eisSyncState ?? source.eis_sync_state).toLowerCase();
  const state = toTrimmedString(
    submission.state ??
      submission.submission_state ??
      source.submissionState ??
      source.submission_state ??
      source.fiscalReceiptState ??
      source.fiscal_receipt_state
  ).toLowerCase();
  const submissionStatus = toTrimmedString(
    submission.status ?? source.eisSubmissionStatus ?? source.eis_submission_status ?? source.fiscal_receipt_status
  ).toLowerCase();
  const submissionEisStatus = toTrimmedString(
    submission.eis_status ?? submission.eisStatus
  ).toUpperCase();
  const fiscalInvoice = toTrimmedString(
    source.fiscalInvoiceNumber ?? source.fiscal_invoice_number
  );

  if (resolveEisSubmissionFailureReason(source)) return 'Fiscal Failed';

  if (
    status === 'SUBMITTED' ||
    status === 'ACCEPTED' ||
    ['submitted', 'accepted'].includes(syncState) ||
    ['submitted', 'accepted'].includes(state) ||
    submissionStatus === 'submitted' ||
    submissionStatus === 'accepted' ||
    submissionEisStatus === 'SUBMITTED' ||
    submissionEisStatus === 'ACCEPTED'
  ) {
    return 'EIS Submitted';
  }

  if (
    status === 'PENDING' ||
    ['pending', 'queued', 'offline_queued', 'retrying'].includes(syncState) ||
    ['pending', 'queued', 'offline_queued', 'retrying'].includes(state) ||
    ['pending', 'queued', 'retrying'].includes(submissionStatus) ||
    (eisEnabled && Boolean(source._dirty))
  ) {
    return 'EIS Pending';
  }

  if (
    Boolean(fiscalInvoice)
  ) {
    return 'EIS Submitted';
  }

  return '';
};
