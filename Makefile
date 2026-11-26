.PHONY: all clean migr benchmark fetch

all: migr benchmark

migr:
	$(MAKE) -C migr

benchmark:
	$(MAKE) -C benchmark

fetch:
	$(MAKE) -C benchmark fetch

clean:
	$(MAKE) -C migr clean
	$(MAKE) -C benchmark clean