.PHONY: all clean migr benchmark

all: migr benchmark

migr:
	$(MAKE) -C migr

benchmark:
	$(MAKE) -C benchmark

clean:
	$(MAKE) -C migr clean
	$(MAKE) -C benchmark clean