import math
from datetime import datetime

from jsonschema import Draft202012Validator

from .errors import OCRException


def scalar_fields(value, path=''):
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in {'field_confidence', 'overall_confidence', 'overall_accuracy_percentage', 'review_required', 'needs_review', 'confidence'}:
                yield from scalar_fields(item, path + '/' + key.replace('~', '~0').replace('/', '~1'))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from scalar_fields(item, path + '/' + str(index))
    elif value is not None:
        yield path, value


def validate_extraction(data, config):
    if list(Draft202012Validator(config['schema']).iter_errors(data)):
        raise OCRException(502, 'schema_validation_failed', 'Provider output does not match the extraction schema.')
    # JSON Schema's number type can accept non-finite floats in Python; reject them explicitly.
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise OCRException(502, 'invalid_number', 'Provider output contains a non-finite number.')
        if isinstance(value, dict):
            for item in value.values(): finite(item)
        elif isinstance(value, list):
            for item in value: finite(item)
    finite(data)
    review = config['review']
    expected = dict(scalar_fields(data))
    entries = data['field_confidence']
    paths = [entry['field_path'] for entry in entries]
    if len(set(paths)) != len(paths) or set(paths) != set(expected):
        raise OCRException(502, 'confidence_coverage_invalid', 'Every extracted scalar must have exactly one field confidence entry.')
    for entry in entries:
        if expected[entry['field_path']] == '':
            entry['confidence'] = 0
        entry['accuracy_percentage'] = round(entry['confidence'] * review['percentage_scale'], review['percentage_precision'])
        if entry['confidence'] < review['confidence_threshold']:
            data['review_required'] = True
    data['overall_accuracy_percentage'] = round(data['overall_confidence'] * review['percentage_scale'], review['percentage_precision'])
    if data['overall_confidence'] < review['confidence_threshold']:
        data['review_required'] = True
    confidence = {entry['field_path']: entry['confidence'] for entry in entries}
    for index, medicine in enumerate(data['medications']):
        if (any(not medicine[key].strip() for key in review['required_medication_fields'])
            or medicine['confidence'] is None or medicine['confidence'] < review['confidence_threshold']
            or any(score < review['confidence_threshold'] for path, score in confidence.items() if path.startswith(f'/medications/{index}/'))):
            medicine['needs_review'] = True
        if medicine['needs_review']:
            data['review_required'] = True
    prescription = data['prescription']
    if prescription and prescription['date'] is not None:
        try:
            parsed = datetime.strptime(prescription['date'], review['date_format'])
            if parsed.strftime(review['date_format']) != prescription['date']:
                raise ValueError('Date format')
        except ValueError as exc:
            raise OCRException(502, 'invalid_prescription_date', 'Provider date must be null or a valid DD-MM-YYYY date.') from exc
    return data
