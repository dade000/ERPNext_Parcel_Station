class ParcelError(Exception):
    """Base app error."""

class CarrierUnavailable(ParcelError):
    pass

class CarrierAuthError(ParcelError):
    pass

class CarrierValidationError(ParcelError):
    pass

class CarrierNetworkError(ParcelError):
    pass
